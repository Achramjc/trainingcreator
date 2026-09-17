"""
Real pytest assertions for SOPParser, covering the M0 content-fidelity fixes:
  - full procedure step bodies (no longer dropped at the first blank line)
  - correct definitions parsing (terms with parentheses/hyphens/slashes)
  - responsibilities extraction
  - heading-driven purpose/scope extraction
  - safety warnings (deduped, in document order, wrapped-line support)
"""

import os

import pytest

from src.parser import SOPParser

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_SOP = os.path.join(REPO_ROOT, "examples", "sample_sop.txt")
SAMPLE_SOP_NUMBERED = os.path.join(REPO_ROOT, "examples", "sample_sop_numbered.txt")


@pytest.fixture(scope="module")
def sample_sop():
    return SOPParser().parse(SAMPLE_SOP)


@pytest.fixture(scope="module")
def numbered_sop():
    return SOPParser().parse(SAMPLE_SOP_NUMBERED)


# ---------------------------------------------------------------------------
# Procedures
# ---------------------------------------------------------------------------

class TestProcedures:
    def test_nine_procedures(self, sample_sop):
        assert len(sample_sop.procedures) == 9

    def test_step_numbers_in_order(self, sample_sop):
        assert [p["step_number"] for p in sample_sop.procedures] == [str(n) for n in range(1, 10)]

    def test_step_2_title(self, sample_sop):
        step2 = sample_sop.procedures[1]
        assert step2["step_number"] == "2"
        assert step2["title"] == "Activate Emergency Stop"

    def test_step_2_body_and_substeps(self, sample_sop):
        step2 = sample_sop.procedures[1]
        assert "Press the red emergency stop button" in step2["body"]
        assert len(step2["substeps"]) == 3
        assert "cut power" in step2["substeps"][0]

    def test_step_2_content_key_has_full_text(self, sample_sop):
        # 'content' is unchanged as a key, but must now be the FULL step text.
        step2 = sample_sop.procedures[1]
        assert "Activate Emergency Stop" in step2["content"]
        assert "Press the red emergency stop button" in step2["content"]

    def test_every_step_body_nonempty(self, sample_sop):
        for proc in sample_sop.procedures:
            assert proc["body"].strip() != "", f"step {proc['step_number']} has an empty body"

    def test_source_lines_increasing_and_in_file(self, sample_sop):
        with open(SAMPLE_SOP, encoding="utf-8") as f:
            total_lines = len(f.readlines())

        prev_end = 0
        for proc in sample_sop.procedures:
            start, end = proc["source_lines"]
            assert start <= end
            assert start > prev_end
            assert 1 <= start <= total_lines
            assert 1 <= end <= total_lines
            prev_end = end

    def test_downstream_keys_preserved(self, sample_sop):
        # Other agents' code depends on proc.get('content') / proc.get('step_number').
        for proc in sample_sop.procedures:
            assert "content" in proc
            assert "step_number" in proc
            assert "substeps" in proc


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------

class TestDefinitions:
    def test_four_definitions(self, sample_sop):
        assert len(sample_sop.definitions) == 4

    def test_term_with_parens_and_hyphen(self, sample_sop):
        assert "Emergency Shutdown (E-Stop)" in sample_sop.definitions
        assert sample_sop.definitions["Emergency Shutdown (E-Stop)"].startswith("An immediate cessation")

    def test_term_with_slash(self, sample_sop):
        assert "Lockout/Tagout (LOTO)" in sample_sop.definitions


# ---------------------------------------------------------------------------
# Responsibilities
# ---------------------------------------------------------------------------

class TestResponsibilities:
    def test_four_responsibilities(self, sample_sop):
        assert len(sample_sop.responsibilities) == 4

    def test_first_responsibility(self, sample_sop):
        assert "Line Operators" in sample_sop.responsibilities[0]


# ---------------------------------------------------------------------------
# Purpose / Scope
# ---------------------------------------------------------------------------

class TestPurposeScope:
    def test_purpose_is_complete_sentence(self, sample_sop):
        assert sample_sop.purpose.endswith("during emergency situations.")

    def test_scope_contains_expected_phrases(self, sample_sop):
        assert "Building A" in sample_sop.scope
        assert "immediate line stoppage" in sample_sop.scope


# ---------------------------------------------------------------------------
# Safety warnings
# ---------------------------------------------------------------------------

class TestSafetyWarnings:
    def test_four_warnings_in_order(self, sample_sop):
        assert len(sample_sop.safety_warnings) == 4
        assert sample_sop.safety_warnings[0].startswith("Never attempt to restart")

    def test_note_is_not_a_warning(self):
        content = (
            "PROCEDURE:\n\n"
            "Step 1: Do Something\n"
            "WARNING: Wear gloves at all times.\n"
            "NOTE: This is just a note, not a warning.\n"
        )
        sop = SOPParser()._extract_structure(content)
        assert len(sop.safety_warnings) == 1
        assert "gloves" in sop.safety_warnings[0]

    def test_wrapped_warning_captured_whole(self):
        content = (
            "WARNING: This warning wraps across\n"
            "a second physical line of text.\n"
            "\n"
            "PROCEDURE:\n"
            "Step 1: Something\n"
            "Body text.\n"
        )
        sop = SOPParser()._extract_structure(content)
        assert len(sop.safety_warnings) == 1
        assert "wraps across" in sop.safety_warnings[0]
        assert "second physical line" in sop.safety_warnings[0]

    def test_duplicate_warnings_deduped(self):
        content = (
            "WARNING: Duplicate warning text.\n"
            "WARNING: Duplicate warning text.\n"
            "WARNING: Different warning text.\n"
        )
        sop = SOPParser()._extract_structure(content)
        assert sop.safety_warnings == ["Duplicate warning text.", "Different warning text."]


# ---------------------------------------------------------------------------
# Regression: multi-paragraph step body must not be cut at a blank line
# ---------------------------------------------------------------------------

class TestStepBodySpansBlankLine:
    def test_body_spans_blank_line(self):
        content = (
            "PROCEDURE:\n"
            "\n"
            "Step 1: Do The Thing\n"
            "First paragraph line one.\n"
            "First paragraph line two.\n"
            "\n"
            "Second paragraph after a blank line.\n"
            "\n"
            "Step 2: Another Step\n"
            "Body two.\n"
        )
        sop = SOPParser()._extract_structure(content)
        assert len(sop.procedures) == 2

        step1 = sop.procedures[0]
        assert "First paragraph line one." in step1["body"]
        assert "First paragraph line two." in step1["body"]
        assert "Second paragraph after a blank line." in step1["body"]

        step2 = sop.procedures[1]
        assert step2["body"].strip() == "Body two."


# ---------------------------------------------------------------------------
# Second fixture: numbered-heading SOP convention
# ---------------------------------------------------------------------------

class TestNumberedConventionFixture:
    def test_purpose_and_scope(self, numbered_sop):
        assert "calibrate the widget press" in numbered_sop.purpose
        assert "Building B" in numbered_sop.scope

    def test_definitions(self, numbered_sop):
        assert len(numbered_sop.definitions) == 2
        assert "Calibration Target (CT)" in numbered_sop.definitions

    def test_bullet_style_responsibilities(self, numbered_sop):
        assert len(numbered_sop.responsibilities) == 2
        assert any("Shift Supervisors" in r for r in numbered_sop.responsibilities)

    def test_safety_warnings(self, numbered_sop):
        assert len(numbered_sop.safety_warnings) == 2

    def test_decimal_numbered_steps(self, numbered_sop):
        step_numbers = [p["step_number"] for p in numbered_sop.procedures]
        assert step_numbers == ["4.1", "4.2", "4.3"]

    def test_step_with_lettered_substeps(self, numbered_sop):
        step = next(p for p in numbered_sop.procedures if p["step_number"] == "4.2")
        assert step["title"] == "Attach Calibration Target"
        assert len(step["substeps"]) == 2
        assert "Align the target" in step["substeps"][0]


# ---------------------------------------------------------------------------
# Markdown input still works
# ---------------------------------------------------------------------------

class TestProvenance:
    """Every extracted field carries a source-line span, and every span
    resolves to text that actually contains what it claims to."""

    def _assert_valid_span(self, sop, span):
        s, e = span
        assert 1 <= s <= e <= len(sop.lines)

    def test_lines_is_the_converted_text(self, sample_sop):
        with open(SAMPLE_SOP, encoding="utf-8") as f:
            raw_lines = f.read().split("\n")
        assert sample_sop.lines == raw_lines

    def test_all_scalar_spans_valid_sample(self, sample_sop):
        for key in ("title", "version", "effective_date", "purpose", "scope"):
            assert key in sample_sop.provenance, key
            self._assert_valid_span(sample_sop, sample_sop.provenance[key])

    def test_all_scalar_spans_valid_numbered(self, numbered_sop):
        for key in ("title", "version", "effective_date", "purpose", "scope"):
            assert key in numbered_sop.provenance, key
            self._assert_valid_span(numbered_sop, numbered_sop.provenance[key])

    def test_purpose_excerpt_contains_expected_text(self, sample_sop):
        span = sample_sop.provenance["purpose"]
        assert "establishes the proper protocol" in sample_sop.excerpt(span)

    def test_control_panel_definition_excerpt(self, sample_sop):
        span = sample_sop.provenance["definitions"]["Control Panel"]
        assert "Station 5" in sample_sop.excerpt(span)

    def test_every_definition_span_valid_and_matches(self, sample_sop):
        for term, span in sample_sop.provenance["definitions"].items():
            self._assert_valid_span(sample_sop, span)
            assert term in sample_sop.excerpt(span)

    def test_every_definition_span_valid_numbered(self, numbered_sop):
        for term, span in numbered_sop.provenance["definitions"].items():
            self._assert_valid_span(numbered_sop, span)
            assert term in numbered_sop.excerpt(span)

    def test_every_responsibility_span_valid(self, sample_sop):
        spans = sample_sop.provenance["responsibilities"]
        assert len(spans) == len(sample_sop.responsibilities)
        for span, text in zip(spans, sample_sop.responsibilities):
            self._assert_valid_span(sample_sop, span)
            # first few words of the responsibility line should appear in the excerpt
            first_words = " ".join(text.split()[:3])
            assert first_words in sample_sop.excerpt(span)

    def test_every_warning_span_valid_and_matches_first_words(self, sample_sop):
        spans = sample_sop.provenance["safety_warnings"]
        assert len(spans) == len(sample_sop.safety_warnings)
        for span, warning in zip(spans, sample_sop.safety_warnings):
            self._assert_valid_span(sample_sop, span)
            first_five = " ".join(warning.split()[:5])
            assert first_five in sample_sop.excerpt(span)

    def test_every_warning_span_valid_numbered(self, numbered_sop):
        spans = numbered_sop.provenance["safety_warnings"]
        assert len(spans) == len(numbered_sop.safety_warnings)
        for span, warning in zip(spans, numbered_sop.safety_warnings):
            self._assert_valid_span(numbered_sop, span)
            first_five = " ".join(warning.split()[:5])
            assert first_five in numbered_sop.excerpt(span)

    def test_excerpt_with_context(self, sample_sop):
        span = sample_sop.provenance["purpose"]
        no_context = sample_sop.excerpt(span)
        with_context = sample_sop.excerpt(span, context=1)
        assert len(with_context) >= len(no_context)

    def test_excerpt_empty_span(self, sample_sop):
        assert sample_sop.excerpt(None) == ""
        assert sample_sop.excerpt([]) == ""

    def test_procedure_source_lines_still_valid(self, sample_sop):
        for proc in sample_sop.procedures:
            self._assert_valid_span(sample_sop, proc["source_lines"])

    def test_to_dict_includes_provenance_and_lines(self, sample_sop):
        d = sample_sop.to_dict()
        assert d["provenance"] == sample_sop.provenance
        assert d["lines"] == sample_sop.lines


# ---------------------------------------------------------------------------
# Regression: a bare ALL-CAPS line inside a step body must not be mistaken
# for a section heading (e.g. an acronym like "LOTO" or a shouted
# instruction like "PRESS THE E-STOP").
# ---------------------------------------------------------------------------

class TestAllCapsInsideStepBody:
    def test_all_caps_acronym_line_stays_in_step_body(self):
        content = (
            "PROCEDURE:\n\n"
            "Step 1: Perform Lockout\n"
            "Apply LOTO before servicing the press.\n"
            "PRESS THE E-STOP if anything moves unexpectedly.\n"
            "Continue with the lockout checklist.\n\n"
            "Step 2: Verify Zero Energy\n"
            "Confirm the gauge reads zero.\n"
        )
        sop = SOPParser()._extract_structure(content)
        assert len(sop.procedures) == 2

        step1 = sop.procedures[0]
        assert "Apply LOTO before servicing the press." in step1["body"]
        assert "PRESS THE E-STOP if anything moves unexpectedly." in step1["body"]
        assert "Continue with the lockout checklist." in step1["body"]

        step2 = sop.procedures[1]
        assert step2["body"].strip() == "Confirm the gauge reads zero."

    def test_all_caps_still_ends_section_outside_procedure(self):
        # A generic ALL-CAPS heading OUTSIDE a procedure body (e.g. closing
        # out a document with an unrecognised heading) still behaves as a
        # section boundary.
        content = (
            "PURPOSE:\n"
            "Do the thing.\n\n"
            "RANDOM NOTES\n"
            "This should not be part of the purpose.\n"
        )
        sop = SOPParser()._extract_structure(content)
        assert "Do the thing." in sop.purpose
        assert "RANDOM NOTES" not in sop.purpose
        assert "should not be part" not in sop.purpose


class TestMarkdownParsing:
    def test_markdown_headings(self, tmp_path):
        md_content = (
            "## Purpose\n"
            "This is the purpose text for the markdown SOP.\n\n"
            "## Scope\n"
            "Applies to widgets in the Markdown Zone.\n\n"
            "## Procedure\n\n"
            "### Step 1: Do The Thing\n"
            "First paragraph line one.\n\n"
            "Step 2: Another Step\n"
            "Body two.\n"
        )
        md_file = tmp_path / "sop.md"
        md_file.write_text(md_content, encoding="utf-8")

        sop = SOPParser().parse(str(md_file))

        assert "purpose text for the markdown SOP" in sop.purpose
        assert "Markdown Zone" in sop.scope
        assert len(sop.procedures) == 2
        assert sop.procedures[0]["title"] == "Do The Thing"
        assert "First paragraph line one" in sop.procedures[0]["body"]
