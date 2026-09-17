"""Tests for the optional, grounded LLM enhancement layer (`src/llm`).

NO TEST IN THIS FILE MAKES A NETWORK CALL.  Everything runs against
``FakeProvider``, which returns scripted results.  The single live smoke test at
the bottom is skipped unless BOTH ``ANTHROPIC_API_KEY`` and
``TRAINING_CREATOR_LLM_LIVE_TESTS=1`` are set, and it is the only test in the
repository allowed to leave the machine.

What is actually being defended here:

* the grounding check rejects the things that would end this product in an
  audit - an invented number, an unsupported paraphrase, a citation that does
  not resolve, an invented instruction appended to a faithfully quoted one, and
  a "wrong answer" the document actually states;
* it fails CLOSED: heavy paraphrases are rejected too, and there are tests
  saying so on purpose;
* its documented blind spots stay documented - two characterization tests pin
  the cases docs/LLM.md admits it cannot catch, so they cannot change silently;
* enhancement never mutates its inputs and never crashes the pipeline;
* M0's non-negotiable invariants survive the layer: the answer key still does
  not reach the learner, and a naive learner still fails - including after
  distractors have been swapped, which changes the option length profile M0
  spent real effort shaping.
"""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.assessments import AssessmentGenerator, MedicalDeviceAssessmentGenerator
from src.generator import TrainingGenerator
from src.llm import (
    LLMConfig,
    EnhancementReport,
    FakeProvider,
    NullProvider,
    ProviderResult,
    build_provider,
    document_asserts,
    enhance_assessment,
    enhance_module,
    format_document,
    resolve_span,
    span_excerpt,
    system_blocks,
    verify_claim,
    verify_distractor,
    worst_naive_score,
)
from src.llm.config import (
    ENV_API_KEY,
    ENV_ENABLE,
    ENV_LIVE_TESTS,
    ENV_MODEL,
    MAX_UNSUPPORTED_CONTENT_WORDS,
    MIN_CONTENT_WORD_OVERLAP,
)
from src.llm.enhance import TASK_DISTRACTORS, TASK_OBJECTIVES, TASK_SUMMARIES
from src.parser import SOPParser

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ("sample_sop.txt", "sample_sop_numbered.txt")
GENERATORS = ("standard", "medical_device")

ON = LLMConfig(backend="anthropic")

# Lines 48-49 of examples/sample_sop.txt are Step 5 and its body:
#   L0048: Step 5: Document the Incident
#   L0049: The Line Supervisor must complete an Emergency Shutdown Report
#          (Form MS-101) within 30 minutes of the event. ...
STEP5_SPAN = [48, 49]
# Lines 25-28 are the SAFETY WARNINGS block.
SAFETY_SPAN = [25, 28]
# Lines 33-34 are Step 1 and its body ("... Alert nearby personnel ...").
STEP1_SPAN = [33, 34]
# Lines 36-40 are Step 2 ("Activate Emergency Stop"), its body and its
# lettered sub-steps.
STEP2_SPAN = [36, 40]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _parse(doc_name):
    return SOPParser().parse(str(REPO_ROOT / "examples" / doc_name))


@pytest.fixture(scope="module")
def sop():
    return _parse("sample_sop.txt")


@pytest.fixture
def module(sop):
    return TrainingGenerator().generate(sop)


@pytest.fixture
def assessment(sop):
    return AssessmentGenerator().generate(sop, num_questions=6)


def _make_assessment(doc_name, generator_name, num_questions=6):
    content = _parse(doc_name)
    generator = (AssessmentGenerator() if generator_name == "standard"
                 else MedicalDeviceAssessmentGenerator())
    return content, generator.generate(content, num_questions=num_questions)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_config_defaults_to_off():
    config = LLMConfig.from_env({})
    assert config.backend == "off"
    assert config.enabled is False
    assert config.model == "claude-opus-5"


def test_config_reads_backend_and_model():
    config = LLMConfig.from_env({ENV_ENABLE: "anthropic",
                                 ENV_MODEL: "claude-opus-5",
                                 ENV_API_KEY: "sk-ant-not-a-real-key"})
    assert config.enabled is True
    assert config.api_key_present is True
    assert config.notes == ()


def test_config_never_carries_the_api_key():
    secret = "sk-ant-super-secret-value"
    config = LLMConfig.from_env({ENV_ENABLE: "anthropic", ENV_API_KEY: secret})
    assert secret not in json.dumps(config.to_dict())
    assert config.api_key_present is True


def test_config_unknown_backend_stays_off_and_says_so():
    config = LLMConfig.from_env({ENV_ENABLE: "openai"})
    assert config.enabled is False
    assert any("openai" in note for note in config.notes)


def test_config_missing_key_is_a_note_not_a_failure():
    config = LLMConfig.from_env({ENV_ENABLE: "anthropic"})
    assert config.enabled is True
    assert config.api_key_present is False
    assert any(ENV_API_KEY in note for note in config.notes)


def test_llm_flag_promotes_off_to_anthropic():
    assert LLMConfig.from_env({}).with_enabled(True).backend == "anthropic"
    assert LLMConfig.from_env({ENV_ENABLE: "anthropic"}).with_enabled(False).backend \
        == "off"


def test_build_provider_off_gives_null_provider():
    assert isinstance(build_provider(LLMConfig.from_env({})), NullProvider)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
def test_document_is_presented_with_one_indexed_line_numbers(sop):
    rendered = format_document(sop).splitlines()
    assert rendered[0].startswith("L0001: ")
    assert rendered[47].startswith("L0048: ")
    assert "Step 5: Document the Incident" in rendered[47]


def test_system_prefix_is_byte_stable(sop):
    """Any per-run content in the cached blocks silently kills the cache."""
    first = system_blocks(sop)
    second = system_blocks(_parse("sample_sop.txt"))
    assert first == second
    assert all(block["cache_control"] == {"type": "ephemeral"} for block in first)


def test_system_prompt_states_the_grounding_rules(sop):
    prompt = system_blocks(sop)[0]["text"]
    for phrase in ("State only what the document states", "Never introduce a number",
                   "Prefer the document's own wording", "subject-matter expert"):
        assert phrase in prompt


# ---------------------------------------------------------------------------
# Grounding - verify_claim
# ---------------------------------------------------------------------------
def test_span_resolution_and_excerpt(sop):
    assert resolve_span(sop, [48, 49]) == (48, 49)
    assert resolve_span(sop, [49, 48]) == (48, 49)      # tolerated, normalised
    assert resolve_span(sop, [0, 3]) is None
    assert resolve_span(sop, [1, 10 ** 6]) is None
    assert resolve_span(sop, "L48") is None
    assert "Form MS-101" in span_excerpt(sop, STEP5_SPAN, context=1)


def test_claim_with_a_number_not_in_the_excerpt_is_rejected(sop):
    """The highest-value check: a hallucinated figure in a regulated procedure."""
    verdict = verify_claim(
        "Complete the Emergency Shutdown Report within 15 minutes of the event.",
        sop, STEP5_SPAN)
    assert verdict.ok is False
    assert any("15" in reason for reason in verdict.reasons)

    supported = verify_claim(
        "Complete the Emergency Shutdown Report within 30 minutes of the event.",
        sop, STEP5_SPAN)
    assert supported.ok is True, supported.reasons


def test_light_paraphrase_in_the_documents_own_words_is_accepted(sop):
    """Accepted, but only just: the bar is deliberately close to quotation."""
    verdict = verify_claim(
        "Press the red E-STOP button at the nearest workstation; buttons are "
        "spaced 50 feet apart along the line.", sop, STEP2_SPAN)
    assert verdict.ok is True, verdict.reasons
    # A real paraphrase, not a copy - the threshold is doing work.
    assert MIN_CONTENT_WORD_OVERLAP <= verdict.detail["content_word_overlap"] < 1.0
    assert verdict.detail["unsupported_words"] == ["buttons", "spaced", "apart"]


def test_heavy_paraphrase_is_rejected_and_that_is_intended(sop):
    """A fluent restatement that drops the document's wording is REJECTED.

    This is not a bug to be tuned away. The prompt asks for the document's own
    wording, and the check fails closed: a false rejection only keeps the
    deterministic original, whereas a false acceptance puts unverifiable text
    into regulated training. Note also that "five seconds" would sail past the
    numbers rule - the document says "5 seconds" - which is precisely why the
    wording rules are not optional.
    """
    verdict = verify_claim(
        "Hit the emergency stop control closest to you; all moving equipment "
        "loses power within five seconds.", sop, STEP2_SPAN)
    assert verdict.ok is False
    assert verdict.detail["content_word_overlap"] < MIN_CONTENT_WORD_OVERLAP
    assert len(verdict.detail["unsupported_words"]) > MAX_UNSUPPORTED_CONTENT_WORDS
    assert any("limit 3" in reason for reason in verdict.reasons)


def test_paraphrase_far_below_the_overlap_threshold_is_rejected(sop):
    verdict = verify_claim(
        "Supervisors log the shutdown details on Form MS-101 promptly after "
        "every incident.", sop, STEP5_SPAN)
    assert verdict.ok is False
    assert verdict.detail["content_word_overlap"] < 0.5


def test_an_invented_step_appended_to_a_grounded_sentence_is_rejected(sop):
    """The failure a ratio alone cannot see.

    The first half is quoted faithfully from the cited lines and pays for the
    second half, which is invented. At a 50% bar this sentence was ACCEPTED -
    an instruction to call the fire department, in a controlled procedure that
    says no such thing, carrying a citation that looks legitimate.
    """
    verdict = verify_claim(
        "Press the red E-STOP button and then call the fire department.",
        sop, STEP2_SPAN)

    assert verdict.ok is False
    assert verdict.detail["added_clause_words"] == ["call", "fire", "department"]
    added_reason = [r for r in verdict.reasons if "adds an action" in r]
    assert added_reason, verdict.reasons
    for word in ("call", "fire", "department"):
        assert word in added_reason[0]


def test_an_added_clause_is_caught_even_when_the_ratio_would_pass(sop):
    """Rule (3c) is independent of the ratio, not a restatement of it."""
    verdict = verify_claim(
        "Press the red emergency stop button located at your nearest "
        "workstation and notify the fire marshal.", sop, STEP2_SPAN)

    assert verdict.detail["content_word_overlap"] >= MIN_CONTENT_WORD_OVERLAP
    assert verdict.ok is False
    assert verdict.detail["added_clause_words"] == ["notify", "fire", "marshal"]


def test_an_elaboration_of_supported_material_is_not_an_added_step(sop):
    """A clause after "and" with even one supported word is not an addition."""
    verdict = verify_claim(
        "The emergency stop will cut power to all moving equipment and audible "
        "alarms will activate.", sop, STEP2_SPAN)
    assert verdict.detail["added_clause_words"] == []
    assert verdict.ok is True, verdict.reasons


def test_known_blind_spot_added_clause_built_from_present_words(sop):
    """DOCUMENTED LIMITATION, asserted so it cannot change silently.

    The added-step rule fires only when EVERY content word of the added clause
    is missing from the excerpt. Step 2 says nothing about notifying anyone,
    but "line" appears in the cited window ("production line"), so the clause
    is read as an elaboration and the sentence passes. Recombining the
    document's own vocabulary into an instruction it never gives is a failure
    a lexical check cannot see. See docs/LLM.md, "What the check cannot catch",
    and note that this is a standing reason SME approval is mandatory.
    """
    verdict = verify_claim(
        "Press the red emergency stop button and then notify the Line "
        "Supervisor.", sop, STEP2_SPAN)
    assert verdict.ok is True
    assert verdict.detail["unsupported_words"] == ["notify", "supervisor"]
    assert verdict.detail["added_clause_words"] == []


def test_known_blind_spot_swapped_actor(sop):
    """DOCUMENTED LIMITATION: the document assigns Form MS-101 to the Line
    Supervisor, not the Safety Officer, but both roles are the document's own
    vocabulary and the swap is semantic, not lexical."""
    verdict = verify_claim(
        "The Safety Officer must complete an Emergency Shutdown Report within "
        "30 minutes of the event.", sop, STEP5_SPAN)
    assert verdict.ok is True
    assert verdict.detail["unsupported_words"] == ["safety", "officer"]


def test_named_terms_do_not_buy_extra_unsupported_words(sop):
    """The defined-terms alternative must not override the absolute cap."""
    verdict = verify_claim(
        "The Line Supervisor files the Emergency Shutdown Report on Form "
        "MS-101 whenever Maintenance escalates a Category Four stoppage.",
        sop, STEP5_SPAN)
    assert verdict.ok is False
    assert len(verdict.detail["unsupported_words"]) > MAX_UNSUPPORTED_CONTENT_WORDS
    assert any("limit {0}".format(MAX_UNSUPPORTED_CONTENT_WORDS) in reason
               for reason in verdict.reasons)


def test_claim_with_an_out_of_range_span_is_rejected(sop):
    verdict = verify_claim("The Line Supervisor completes the report.",
                           sop, [900, 905])
    assert verdict.ok is False
    assert any("does not resolve" in reason for reason in verdict.reasons)


def test_wholly_unsupported_claim_is_rejected(sop):
    verdict = verify_claim(
        "Operators must wear insulated gloves and a face shield at all times.",
        sop, STEP5_SPAN)
    assert verdict.ok is False


def test_empty_and_overlong_claims_are_rejected(sop):
    assert verify_claim("", sop, STEP5_SPAN).ok is False
    assert verify_claim("   ", sop, STEP5_SPAN).ok is False
    long_claim = ("The Line Supervisor must complete an Emergency Shutdown "
                  "Report Form MS-101 within 30 minutes of the event. ") * 4
    verdict = verify_claim(long_claim, sop, STEP5_SPAN)
    assert verdict.ok is False
    assert any("characters" in reason for reason in verdict.reasons)


# ---------------------------------------------------------------------------
# Grounding - verify_distractor
# ---------------------------------------------------------------------------
CORRECT_WARNING = ("Never attempt to restart equipment until a complete safety "
                   "inspection has been performed and documented.")


def test_distractor_the_document_asserts_is_rejected(sop):
    """A 'wrong answer' the SOP states is a broken question, not a hard one."""
    real_warning = ("All personnel must evacuate the immediate production area "
                    "during emergency shutdown.")
    assert document_asserts(real_warning, sop) is not None
    verdict = verify_distractor(real_warning, CORRECT_WARNING, sop)
    assert verdict.ok is False
    assert any("document" in reason.lower() for reason in verdict.reasons)


def test_distractor_that_contradicts_the_document_is_accepted(sop):
    verdict = verify_distractor(
        "Always restart the equipment immediately, without waiting for a "
        "safety inspection.", CORRECT_WARNING, sop)
    assert verdict.ok is True, verdict.reasons


def test_distractor_equal_to_or_near_the_correct_answer_is_rejected(sop):
    assert verify_distractor(CORRECT_WARNING, CORRECT_WARNING, sop).ok is False
    near = CORRECT_WARNING.replace("Never", "Never ever")
    assert verify_distractor(near, CORRECT_WARNING, sop).ok is False
    assert verify_distractor("", CORRECT_WARNING, sop).ok is False


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def test_null_provider_reports_disabled():
    result = NullProvider().complete_json([], "", {})
    assert result.ok is False and result.data is None and result.error


def test_fake_provider_returns_scripted_data_in_order():
    provider = FakeProvider([{"a": 1}, {"b": 2}])
    assert provider.complete_json([], "p1", {}).data == {"a": 1}
    assert provider.complete_json([], "p2", {}).data == {"b": 2}
    exhausted = provider.complete_json([], "p3", {})
    assert exhausted.ok is False and "exhausted" in exhausted.error
    assert [call["user_prompt"] for call in provider.calls] == ["p1", "p2", "p3"]


def test_anthropic_provider_import_is_lazy():
    """`src.llm` must import, and the whole suite must run, without the SDK.

    Checked in a clean subprocess: importing the package - and building a
    provider from it - must not pull `anthropic` into `sys.modules`.  A
    module-level import here would make the CLI, the web app and every test in
    the repository depend on an optional package.
    """
    import subprocess
    import sys

    script = (
        "import sys; "
        "import src.llm as L; "
        "L.build_provider(L.LLMConfig(backend='anthropic')); "
        "print('anthropic' in sys.modules)"
    )
    output = subprocess.run([sys.executable, "-c", script], cwd=str(REPO_ROOT),
                            capture_output=True, text=True, check=True)
    assert output.stdout.strip() == "False", output.stdout


# -- AnthropicProvider response handling, with an injected client -----------
#
# These exercise the real provider's parsing and error branches WITHOUT a
# network call: the SDK is imported (for its exception classes) but the HTTP
# client is replaced with a stub.  They are skipped when the optional SDK is
# absent.
class _StubBlock:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _StubUsage:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _StubResponse:
    def __init__(self, content, stop_reason="end_turn", usage=None,
                 stop_details=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage
        self.stop_details = stop_details


class _StubMessages:
    def __init__(self, outcome):
        self._outcome = outcome
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _StubClient:
    def __init__(self, outcome):
        self.messages = _StubMessages(outcome)


def _provider_with(outcome):
    pytest.importorskip("anthropic")
    from src.llm import AnthropicProvider

    provider = AnthropicProvider(ON)
    provider._client = _StubClient(outcome)
    return provider


def test_anthropic_provider_parses_json_and_records_usage():
    usage = _StubUsage(input_tokens=1200, cache_read_input_tokens=1100,
                       cache_creation_input_tokens=0, output_tokens=340)
    provider = _provider_with(_StubResponse(
        [_StubBlock("thinking"), _StubBlock("text", '{"objectives": []}')],
        usage=usage))

    result = provider.complete_json([{"type": "text", "text": "sys"}], "go", {})

    assert result.ok and result.data == {"objectives": []}
    assert result.usage == {"input_tokens": 1200, "cache_read_input_tokens": 1100,
                            "cache_creation_input_tokens": 0, "output_tokens": 340}
    sent = provider._client.messages.kwargs
    assert sent["model"] == ON.model
    assert sent["max_tokens"] == ON.max_tokens
    assert sent["output_config"] == {"format": {"type": "json_schema", "schema": {}}}
    assert "thinking" not in sent            # adaptive by default on claude-opus-5
    assert sent["messages"] == [{"role": "user", "content": "go"}]


def test_anthropic_provider_treats_a_refusal_as_no_enhancement():
    details = _StubUsage(category="cyber", explanation="declined")
    provider = _provider_with(_StubResponse(
        [], stop_reason="refusal", stop_details=details,
        usage=_StubUsage(input_tokens=10, output_tokens=0)))

    result = provider.complete_json([], "go", {})

    assert result.ok is False and result.refused is True
    assert "cyber" in result.error
    assert result.usage["input_tokens"] == 10


def test_anthropic_provider_rejects_non_json_text():
    provider = _provider_with(_StubResponse([_StubBlock("text", "sorry, prose")]))
    result = provider.complete_json([], "go", {})
    assert result.ok is False and "not valid JSON" in result.error


def test_anthropic_provider_maps_sdk_errors_to_reasons():
    anthropic = pytest.importorskip("anthropic")

    class _Resp:
        status_code = 503
        headers = {}
        request = None

    auth = anthropic.AuthenticationError("bad key", response=_Resp(), body=None)
    assert "Authentication failed" in _provider_with(auth).complete_json(
        [], "go", {}).error

    rate = anthropic.RateLimitError("slow down", response=_Resp(), body=None)
    assert "Rate limited" in _provider_with(rate).complete_json([], "go", {}).error

    status = anthropic.APIStatusError("upstream", response=_Resp(), body=None)
    result = _provider_with(status).complete_json([], "go", {})
    assert "Server error" in result.error and "503" in result.error

    connection = anthropic.APIConnectionError(request=None)
    assert "Could not reach the API" in _provider_with(connection).complete_json(
        [], "go", {}).error


# ---------------------------------------------------------------------------
# enhance_module
# ---------------------------------------------------------------------------
GOOD_OBJECTIVE = (
    "Assess the nature and severity of an emergency and determine whether "
    "immediate evacuation is required."
)


def _objectives_payload(entries):
    return {"objectives": list(entries)}


def _summaries_payload(entries):
    return {"summaries": list(entries)}


def test_enhance_module_keeps_accepted_and_reverts_rejected_objectives(sop, module):
    originals = list(module.learning_objectives)
    good = GOOD_OBJECTIVE
    bad = "Press the purple emergency stop button within 90 seconds."

    provider = FakeProvider([
        _objectives_payload([
            {"index": 1, "objective": good, "bloom_level": "apply",
             "span": STEP1_SPAN},
            {"index": 2, "objective": bad, "bloom_level": "apply",
             "span": [36, 37]},
            {"index": 99, "objective": "Out of range.", "bloom_level": "apply",
             "span": STEP1_SPAN},
        ]),
        _summaries_payload([]),
    ])

    enhanced, report = enhance_module(module, sop, provider, ON)

    assert enhanced.learning_objectives[1] == good
    assert enhanced.learning_objectives[2] == originals[2]     # rejected -> kept
    assert module.learning_objectives == originals             # input untouched

    objective_items = [i for i in report.items if i.task == TASK_OBJECTIVES]
    assert len(objective_items) == 3
    assert report.counts_by_task()[TASK_OBJECTIVES] == {"accepted": 1, "rejected": 2}
    assert report.accepted_count == 1 and report.rejected_count == 2
    rejected_reasons = " ".join(i.reason for i in report.rejected)
    assert "90" in rejected_reasons
    assert "Out of range" in " ".join(i.proposed for i in report.rejected)


def test_section_summary_is_injected_and_html_escaped(sop, module):
    """Model text is as untrusted as document text (CLAUDE.md invariant).

    The escaping is belt to the output filter's braces: a summary containing
    ``<script>`` no longer gets as far as this - it is rejected outright with kind
    ``output_filter`` (``tests/test_injection.py``) - so this test uses an
    ampersand, a character that is legitimate in procedure text and still must not
    reach a page unescaped.
    """
    sentences = [
        {"text": "Alert nearby personnel of the emergency situation "
                 "& determine whether immediate evacuation is required.",
         "span": STEP1_SPAN},
        {"text": "Assess the nature and severity of the emergency before "
                 "alerting nearby personnel.",
         "span": STEP1_SPAN},
    ]
    provider = FakeProvider([
        _objectives_payload([]),
        _summaries_payload([{"section_id": "intro", "sentences": sentences}]),
    ])

    enhanced, report = enhance_module(module, sop, provider, ON)

    intro = [s for s in enhanced.sections if s["id"] == "intro"][0]
    assert intro["content"].startswith('<p class="summary">')
    assert "&amp;" in intro["content"]
    assert " & " not in intro["content"]

    original_intro = [s for s in module.sections if s["id"] == "intro"][0]
    assert "summary" not in original_intro["content"]           # input untouched
    assert report.counts_by_task()[TASK_SUMMARIES] == {"accepted": 1, "rejected": 0}


def test_one_unsupported_sentence_rejects_the_whole_summary(sop, module):
    provider = FakeProvider([
        _objectives_payload([]),
        _summaries_payload([{"section_id": "intro", "sentences": [
            {"text": "Assess the nature and severity of the emergency.",
             "span": STEP1_SPAN},
            {"text": "Operators must wear insulated gloves rated to 600 volts.",
             "span": STEP1_SPAN},
        ]}]),
    ])

    enhanced, report = enhance_module(module, sop, provider, ON)

    assert enhanced.to_dict() == module.to_dict()
    item = [i for i in report.items if i.task == TASK_SUMMARIES][0]
    assert item.accepted is False
    assert "Sentence 2" in item.reason


def test_summary_must_be_two_to_four_sentences(sop, module):
    provider = FakeProvider([
        _objectives_payload([]),
        _summaries_payload([{"section_id": "intro", "sentences": [
            {"text": "Assess the nature and severity of the emergency.",
             "span": STEP1_SPAN},
        ]}]),
    ])
    _, report = enhance_module(module, sop, provider, ON)
    item = [i for i in report.items if i.task == TASK_SUMMARIES][0]
    assert item.accepted is False
    assert "2-4 sentences" in item.reason


def test_enhance_module_makes_exactly_two_calls_sharing_a_cache_prefix(sop, module):
    provider = FakeProvider([_objectives_payload([]), _summaries_payload([])])
    enhance_module(module, sop, provider, ON)
    assert len(provider.calls) == 2
    assert provider.calls[0]["system_blocks"] == provider.calls[1]["system_blocks"]


# ---------------------------------------------------------------------------
# enhance_assessment
# ---------------------------------------------------------------------------
GOOD_DISTRACTOR = ("The Line Operator may describe the incident verbally to the "
                   "Safety Officer, and no written record of the shutdown is filed.")


def _distractor_payload(entries):
    return {"questions": list(entries)}


def _question(assessment, question_id):
    return [q for q in assessment.questions if q.id == question_id][0]


def _wrong_options(question):
    return [option for index, option in enumerate(question.options)
            if index != question.correct_answer]


def test_enhance_assessment_replaces_only_accepted_distractors(sop, assessment):
    question = _question(assessment, "step_mc_5")
    correct_before = question.correct_option_text
    wrong = _wrong_options(question)

    provider = FakeProvider([_distractor_payload([{
        "question_id": "step_mc_5",
        "replacements": [
            {"replace": wrong[-1], "with": GOOD_DISTRACTOR,
             "why_false": "The SOP requires a written Form MS-101 report.",
             "span": STEP5_SPAN},
            # The correct option - must never be touched.
            {"replace": correct_before, "with": "Something else entirely.",
             "why_false": "n/a", "span": STEP5_SPAN},
            # A real warning from the document - not a wrong answer.
            {"replace": wrong[0],
             "with": "All personnel must evacuate the immediate production "
                     "area during emergency shutdown.",
             "why_false": "n/a", "span": SAFETY_SPAN},
            # An unresolvable citation.
            {"replace": wrong[1], "with": "Await a written clearance code from "
                                          "the off-site records bureau.",
             "why_false": "n/a", "span": [9000, 9001]},
            # No such option.
            {"replace": "not an option on this question", "with": "x",
             "why_false": "n/a", "span": STEP5_SPAN},
        ],
    }, {"question_id": "no_such_question", "replacements": []}])])

    enhanced, report = enhance_assessment(assessment, sop, provider, ON)
    enhanced_question = _question(enhanced, "step_mc_5")

    assert GOOD_DISTRACTOR in enhanced_question.options
    assert wrong[-1] not in enhanced_question.options
    assert enhanced_question.correct_option_text == correct_before
    assert "Something else entirely." not in enhanced_question.options
    assert wrong[0] in enhanced_question.options
    assert wrong[1] in enhanced_question.options

    # Input untouched.
    assert GOOD_DISTRACTOR not in _question(assessment, "step_mc_5").options

    items = [i for i in report.items if i.task == TASK_DISTRACTORS]
    assert [i.accepted for i in items].count(True) == 1
    reasons = " ".join(i.reason for i in items if not i.accepted)
    assert "correct answer is never changed" in reasons
    assert "does not resolve" in reasons
    assert "No option matching" in reasons


def test_enhanced_assessment_still_hides_the_answer_key(sop, assessment):
    question = _question(assessment, "step_mc_5")
    provider = FakeProvider([_distractor_payload([{
        "question_id": "step_mc_5",
        "replacements": [{"replace": _wrong_options(question)[-1],
                          "with": GOOD_DISTRACTOR,
                          "why_false": "n/a", "span": STEP5_SPAN}],
    }])])

    enhanced, _ = enhance_assessment(assessment, sop, provider, ON)

    payload = json.dumps(enhanced.to_learner_dict())
    assert "correct_answer" not in payload
    assert "explanation" not in payload
    for learner_question in enhanced.to_learner_dict()["questions"]:
        assert learner_question["answer_hash"]


def _length_preserving_distractor(original, tag):
    """A false-for-this-document option about as long as the one it replaces.

    Matching the length matters: M0 defeated "always click the longest option"
    by shaping option lengths, so a test that swapped in wildly different
    lengths would be testing the revert path by accident.
    """
    filler = ("await a written clearance code from the off-site records bureau "
              "and forward a copy to the regional archive desk before any "
              "further action is taken on this line").split()
    words = ["Instead,", tag + ":"]
    index = 0
    while len(" ".join(words)) < len(original) - 8 and index < len(filler):
        words.append(filler[index])
        index += 1
    return " ".join(words)


@pytest.mark.parametrize("generator_name", GENERATORS)
@pytest.mark.parametrize("doc_name", DOCUMENTS)
def test_naive_learner_still_fails_after_enhancement(doc_name, generator_name):
    """M0's exit criterion has to survive the M1 layer, on both fixtures."""
    content, assessment = _make_assessment(doc_name, generator_name)
    proposals = []
    for index, question in enumerate(assessment.questions):
        if question.type != "multiple_choice":
            continue
        wrong = _wrong_options(question)
        if not wrong:
            continue
        proposals.append({
            "question_id": question.id,
            "replacements": [{
                "replace": wrong[-1],
                "with": _length_preserving_distractor(wrong[-1], "option {0}".format(index)),
                "why_false": "Not stated anywhere in this document.",
                "span": [1, 2],
            }],
        })
    assert proposals, "fixture produced no multiple-choice questions"

    provider = FakeProvider([_distractor_payload(proposals)])
    enhanced, report = enhance_assessment(assessment, content, provider, ON)

    assert report.accepted_count >= 1, [i.reason for i in report.rejected]
    strategy, score = worst_naive_score(enhanced)
    assert score < enhanced.passing_score, (
        "{0} scores {1:.1f}% on the enhanced assessment, passing mark is "
        "{2}%".format(strategy, score, enhanced.passing_score))


def test_distractors_that_reopen_the_length_tell_are_reverted(sop, assessment):
    """Short distractors make the correct answer the longest option everywhere.

    That is exactly the defect M0 closed, so the whole set must be rolled back
    rather than shipped with a nicer-sounding wrong answer.
    """
    short_options = [
        "Notify the vendor by email.",
        "Wait for the next shift.",
        "Log the event in a notebook.",
        "Ask a colleague for advice.",
        "Reset the breaker panel.",
        "Call the front desk.",
    ]
    proposals = []
    cursor = 0
    for question in assessment.questions:
        if question.type != "multiple_choice":
            continue
        replacements = []
        for option in _wrong_options(question):
            replacements.append({
                "replace": option,
                "with": short_options[cursor % len(short_options)],
                "why_false": "Not stated in this document.",
                "span": SAFETY_SPAN,
            })
            cursor += 1
        proposals.append({"question_id": question.id,
                          "replacements": replacements})

    before = assessment.to_dict()
    provider = FakeProvider([_distractor_payload(proposals)])
    enhanced, report = enhance_assessment(assessment, sop, provider, ON)

    assert enhanced.to_dict() == before, "the deterministic assessment must be kept"
    assert report.accepted_count == 0
    reverted = [item for item in report.items
                if item.task == TASK_DISTRACTORS and "Reverted" in item.reason]
    assert len(reverted) >= 3, "the individual replacements must say they were reverted"
    assert all(item.accepted is False for item in reverted)
    assert all("always_longest_option" in item.reason for item in reverted)
    notes = " ".join(report.notes)
    assert "reverted" in notes.lower()
    assert "always_longest_option" in notes


def test_true_false_questions_are_left_alone(sop, assessment):
    true_false = [q for q in assessment.questions if q.type == "true_false"]
    assert true_false, "fixture produced no true/false questions"
    provider = FakeProvider([_distractor_payload([{
        "question_id": true_false[0].id,
        "replacements": [{"replace": "True", "with": "Maybe",
                          "why_false": "n/a", "span": SAFETY_SPAN}],
    }])])

    enhanced, report = enhance_assessment(assessment, sop, provider, ON)

    assert enhanced.to_dict() == assessment.to_dict()
    assert report.accepted_count == 0
    assert any("No multiple-choice question" in i.reason for i in report.items)


# ---------------------------------------------------------------------------
# Off switch and failure modes
# ---------------------------------------------------------------------------
def test_off_switch_returns_inputs_unchanged(sop, module, assessment):
    off = LLMConfig.from_env({ENV_ENABLE: "off"})
    assert off.enabled is False

    enhanced_module, module_report = enhance_module(
        module, sop, FakeProvider([{"objectives": []}]), off)
    enhanced_assessment, assessment_report = enhance_assessment(
        assessment, sop, FakeProvider([{"questions": []}]), off)

    assert enhanced_module.to_dict() == module.to_dict()
    assert enhanced_assessment.to_dict() == assessment.to_dict()
    assert module_report.items == [] and module_report.calls == []
    assert assessment_report.items == [] and assessment_report.calls == []
    assert module_report.accepted_count == 0


def test_null_provider_returns_inputs_unchanged(sop, module, assessment):
    enhanced_module, report = enhance_module(module, sop, NullProvider(), ON)
    enhanced_assessment, _ = enhance_assessment(assessment, sop, NullProvider(), ON)
    assert enhanced_module.to_dict() == module.to_dict()
    assert enhanced_assessment.to_dict() == assessment.to_dict()
    assert report.to_dict()["accepted_count"] == 0


def test_missing_provider_returns_inputs_unchanged(sop, module):
    enhanced, report = enhance_module(module, sop, None, ON)
    assert enhanced.to_dict() == module.to_dict()
    assert report.items == []


def test_provider_that_raises_does_not_break_the_pipeline(sop, module, assessment):
    module_provider = FakeProvider([RuntimeError("connection exploded"),
                                    RuntimeError("and again")])
    enhanced_module, module_report = enhance_module(
        module, sop, module_provider, ON)
    assert enhanced_module.to_dict() == module.to_dict()
    assert len(module_report.calls) == 2
    assert all(not call["ok"] for call in module_report.calls)
    assert "connection exploded" in " ".join(module_report.errors)

    assessment_provider = FakeProvider([ValueError("boom")])
    enhanced_assessment, assessment_report = enhance_assessment(
        assessment, sop, assessment_provider, ON)
    assert enhanced_assessment.to_dict() == assessment.to_dict()
    assert "boom" in " ".join(assessment_report.errors)


def test_provider_error_result_is_reported_not_raised(sop, module):
    provider = FakeProvider([
        ProviderResult(data=None, error="Rate limited (429) after 2 retries.",
                       usage={"input_tokens": 12, "output_tokens": 0}),
        ProviderResult(data=None, error="Server error (503): upstream down."),
    ])
    enhanced, report = enhance_module(module, sop, provider, ON)

    assert enhanced.to_dict() == module.to_dict()
    assert report.accepted_count == 0
    assert "Rate limited (429)" in " ".join(report.errors)
    assert report.usage_totals()["input_tokens"] == 12


def test_refusal_is_treated_as_no_enhancement(sop, module):
    provider = FakeProvider([
        ProviderResult(data=None, refused=True,
                       error="The model declined to answer (category: cyber). "
                             "Treated as 'no enhancement'.",
                       usage={"input_tokens": 5, "output_tokens": 0}),
        ProviderResult(data=None, refused=True, error="Declined again."),
    ])
    enhanced, report = enhance_module(module, sop, provider, ON)

    assert enhanced.to_dict() == module.to_dict()
    assert all(call["refused"] for call in report.calls)
    assert "declined" in " ".join(report.errors).lower()


def test_malformed_payloads_are_survived(sop, module, assessment):
    provider = FakeProvider([{"objectives": "not a list"},
                             {"nothing": "useful"}])
    enhanced, report = enhance_module(module, sop, provider, ON)
    assert enhanced.to_dict() == module.to_dict()
    assert report.accepted_count == 0

    provider = FakeProvider([{"questions": [{"question_id": "step_mc_5",
                                             "replacements": "nope"}]}])
    enhanced_assessment, _ = enhance_assessment(assessment, sop, provider, ON)
    assert enhanced_assessment.to_dict() == assessment.to_dict()


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------
def test_report_is_serialisable_and_carries_the_disclosure(sop, module):
    provider = FakeProvider([
        _objectives_payload([{"index": 1, "bloom_level": "apply",
                              "objective": GOOD_OBJECTIVE,
                              "span": STEP1_SPAN}]),
        _summaries_payload([]),
    ])
    _, report = enhance_module(module, sop, provider, ON)
    payload = report.to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert "subject-matter expert" in payload["disclosure"]
    assert payload["accepted_count"] == 1
    assert payload["token_usage"]["cache_read_input_tokens"] > 0
    assert payload["items"][0]["span"] == STEP1_SPAN
    assert "\n".join(report.summary_lines()).startswith("LLM enhancement")


def test_report_summary_lines_when_disabled():
    report = EnhancementReport("module", LLMConfig.from_env({}))
    assert report.summary_lines() == [
        "LLM enhancement: off (deterministic content unchanged)."]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_llm_flag_writes_the_enhancement_report(tmp_path, monkeypatch):
    import src.cli as cli

    scripted = [
        _objectives_payload([{"index": 1, "bloom_level": "apply",
                              "objective": GOOD_OBJECTIVE,
                              "span": STEP1_SPAN}]),
        _summaries_payload([]),
        _distractor_payload([]),
    ]
    provider = FakeProvider(scripted)
    monkeypatch.setattr(cli, "build_llm_provider", lambda config: provider)
    monkeypatch.delenv(ENV_ENABLE, raising=False)

    output_dir = tmp_path / "out"
    result = CliRunner().invoke(cli.main, [
        "--input", str(REPO_ROOT / "examples" / "sample_sop.txt"),
        "--output", str(output_dir),
        "--format", "json",
        "--llm",
    ])

    assert result.exit_code == 0, result.output
    report_path = output_dir / cli.ENHANCEMENT_REPORT_FILENAME
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["scope"] == "package"
    assert payload["accepted_count"] == 1
    assert payload["enabled"] is True
    assert "subject-matter expert" in payload["disclosure"]
    assert len(provider.calls) == 3
    assert "SME must review" in result.output

    exported = json.loads((output_dir / "training.json").read_text(encoding="utf-8"))
    assert exported["training_module"]["learning_objectives"][1] == GOOD_OBJECTIVE


def test_cli_without_the_flag_writes_no_report(tmp_path, monkeypatch):
    import src.cli as cli

    monkeypatch.delenv(ENV_ENABLE, raising=False)

    def explode(config):                       # pragma: no cover - must not run
        raise AssertionError("the LLM layer ran without being asked")

    monkeypatch.setattr(cli, "build_llm_provider", explode)

    output_dir = tmp_path / "out"
    result = CliRunner().invoke(cli.main, [
        "--input", str(REPO_ROOT / "examples" / "sample_sop.txt"),
        "--output", str(output_dir),
        "--format", "json",
    ])

    assert result.exit_code == 0, result.output
    assert not (output_dir / cli.ENHANCEMENT_REPORT_FILENAME).exists()


def test_cli_no_llm_overrides_the_environment(tmp_path, monkeypatch):
    import src.cli as cli

    monkeypatch.setenv(ENV_ENABLE, "anthropic")

    def explode(config):                       # pragma: no cover - must not run
        raise AssertionError("--no-llm did not switch the layer off")

    monkeypatch.setattr(cli, "build_llm_provider", explode)

    output_dir = tmp_path / "out"
    result = CliRunner().invoke(cli.main, [
        "--input", str(REPO_ROOT / "examples" / "sample_sop.txt"),
        "--output", str(output_dir),
        "--format", "json",
        "--no-llm",
    ])

    assert result.exit_code == 0, result.output
    assert not (output_dir / cli.ENHANCEMENT_REPORT_FILENAME).exists()


# ---------------------------------------------------------------------------
# Live smoke test - the only test here that may touch the network
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not (os.environ.get(ENV_API_KEY) and
         os.environ.get(ENV_LIVE_TESTS, "").strip() == "1"),
    reason="set ANTHROPIC_API_KEY and TRAINING_CREATOR_LLM_LIVE_TESTS=1 to run "
           "the live smoke test")
def test_live_smoke_objectives_are_grounded():  # pragma: no cover - opt-in
    """One real call. Asserts an objective was accepted and cites a real span."""
    pytest.importorskip("anthropic")
    from src.llm import AnthropicProvider

    content = _parse("sample_sop.txt")
    training_module = TrainingGenerator().generate(content)
    config = LLMConfig.from_env().with_enabled(True)
    provider = AnthropicProvider(config)

    enhanced, report = enhance_module(training_module, content, provider, config)

    assert report.calls, "no model call was made"
    assert not report.errors, report.errors
    accepted = [i for i in report.accepted if i.task == TASK_OBJECTIVES]
    assert accepted, "no objective survived the grounding check: {0}".format(
        [i.reason for i in report.rejected])

    for item in accepted:
        assert resolve_span(content, item.span) is not None
        assert verify_claim(item.proposed, content, item.span, config).ok
        assert item.proposed in enhanced.learning_objectives

    usage = report.usage_totals()
    assert usage.get("input_tokens", 0) > 0
    assert usage.get("output_tokens", 0) > 0
