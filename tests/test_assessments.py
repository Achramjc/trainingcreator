"""
Regression tests for the M0 assessment-integrity defects.

The exit criterion for M0 is that a deliberately naive learner fails the quiz and
CI proves it.  These tests are that proof, so they are written as properties over
several documents and question counts rather than as spot checks on one sample.

Each test names the defect it guards.

Measured during development over 8,640 generated assessments (documents with
2-9 steps, 0-6 warnings, 0-5 definitions, both generators, num_questions in
{3, 5, 8, 12}), the worst score any fixed strategy achieved was:

    always option 1   62.5%      always the last option   75.0%
    always option 2   75.0%      always True              40.0%
    always option 3   50.0%

and the correct answer sat at each of the four positions 27.0 / 24.8 / 27.3 /
21.0% of the time across 40,290 four-option questions.  The 75% cases are
three-question quizzes generated from two-step documents, which is the
arithmetic floor - with three questions no layout can push any strategy below
about a third.  Everything clears the 80% bar with margin.
"""

import functools
import re
from collections import Counter
from pathlib import Path

import pytest

from src.assessments import (
    MAX_OPTION_CHARS,
    MAX_OPTIONS,
    MIN_ASSESSMENT_QUESTIONS,
    POINTS_CHOICE,
    POINTS_SAFETY_CHOICE,
    POINTS_SAFETY_TRUE_FALSE,
    POINTS_TRUE_FALSE,
    scale_points_for_options,
    AssessmentGenerator,
    MedicalDeviceAssessmentGenerator,
    alter_statement,
    clip_option,
    pick_distractors,
    ordered_steps,
    step_sort_key,
)
from src.parser import SOPContent, SOPParser
from src.samples import list_samples as _list_gallery_samples

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_SOP = REPO_ROOT / "examples" / "sample_sop.txt"
NUMBERED_SOP = REPO_ROOT / "examples" / "sample_sop_numbered.txt"

#: The sample-SOP gallery (examples/gallery/), keyed by id -> resolved path.
#: Folded into DOCUMENTS below so the whole naive-learner / coverage /
#: determinism sweep runs against realistic documents from several
#: regulatory regimes, not just the two original fixtures and the synthetic
#: ones above.
GALLERY_SAMPLES = {entry["id"]: entry["resolved_path"] for entry in _list_gallery_samples()}

#: Every question count the blueprint has to cope with, from the enforced
#: minimum up.  Requests below MIN_ASSESSMENT_QUESTIONS are clamped (see
#: test_short_requests_are_raised_to_the_minimum), so they are not swept here.
QUESTION_COUNTS = tuple(range(MIN_ASSESSMENT_QUESTIONS, 13))
BELOW_MINIMUM_COUNTS = tuple(range(1, MIN_ASSESSMENT_QUESTIONS))

#: Wrong answers shipped by the pre-M0 generator.  Any of these reappearing means
#: the "distractors are giveaways" defect has regressed.
ABSURD_FILLERS = (
    "To increase paperwork requirements",
    "To document historical processes",
    "To ensure compliance with regulatory requirements only",
    "Skip this step if time is limited",
    "Contact supervisor and wait",
    "Document the issue and move to next step",
    "A general industry term",
    "Not defined in this procedure",
    "Optional terminology",
    "Wearing safety equipment is optional",
    "Speed is more important than accuracy",
    "Safety checks can be skipped if in a hurry",
    "True - This is a valid safety warning",
    "False - This is not a concern",
)


# ---------------------------------------------------------------------------
# Synthetic documents
# ---------------------------------------------------------------------------
_WARNINGS = [
    "Never operate the press without the interlock guard fully closed and latched.",
    "All operators must wear cut-resistant gloves when handling the trim blade assembly.",
    "Equipment may remain pressurised after shutdown. Bleed the line before any contact.",
    "Do not bypass the light curtain for any reason, including routine cleaning.",
    "Always verify the lockout tag bears your name before entering the cell.",
]

_DEFINITIONS = [
    ("Interlock Guard",
     "A physical barrier wired to the machine controller that halts motion when opened."),
    ("Trim Blade Assembly",
     "The replaceable cutting head mounted on the secondary station of the press."),
    ("Light Curtain",
     "An optical presence-sensing device that stops hazardous motion when the beam is broken."),
    ("Lockout Tag",
     "A durable identification tag applied to an isolation point naming the person who applied it."),
]

_STEPS = [
    ("Verify Machine State",
     "Confirm the controller reports IDLE and the cycle counter has stopped advancing. "
     "Record the reading on the shift log."),
    ("Isolate Energy Sources",
     "Apply lockout devices to the main disconnect and the pneumatic isolation valve. "
     "Attach your personal lockout tag to each device."),
    ("Verify Zero Energy",
     "Attempt a start cycle from the operator panel and confirm no motion occurs. "
     "Bleed residual pressure at the test port."),
    ("Remove the Trim Blade",
     "Loosen the four retaining bolts in a diagonal pattern and lift the blade assembly "
     "clear using the lifting handle."),
    ("Inspect the Mounting Face",
     "Check the mounting face for scoring, debris and burrs. Clean with approved solvent "
     "and a lint-free wipe."),
    ("Install the Replacement Blade",
     "Seat the new assembly on the locating dowels and torque the retaining bolts to "
     "45 Nm in a diagonal pattern."),
    ("Restore Energy",
     "Remove lockout devices in reverse order, confirming each isolation point is clear "
     "of personnel before restoration."),
    ("Run a Verification Cycle",
     "Execute one dry cycle at reduced speed and confirm the trim dimension is within "
     "tolerance on the first article."),
    ("Record Completion",
     "Complete the maintenance record with the blade serial number, torque values and "
     "first-article results."),
]

_PURPOSE = (
    "This procedure establishes the controlled method for replacing the trim blade "
    "assembly on the secondary press station so that operator injury is prevented and "
    "product dimensional tolerance is maintained."
)
_SCOPE = (
    "This procedure applies to all maintenance technicians and qualified operators "
    "performing trim blade replacement on Press Lines 4 and 5 in Building C. It covers "
    "planned changeovers and unplanned blade failures."
)
_RESPONSIBILITIES = [
    "Maintenance Technicians: perform the blade replacement and complete the maintenance record",
    "Line Supervisors: authorise the shutdown and verify the first article before release",
    "Quality Inspectors: verify trim dimensions and disposition any nonconforming product",
]


def make_sop(title, version, n_steps, n_warnings, n_definitions,
             with_body=True, step_numbers=None):
    """Build an SOPContent by hand.

    ``with_body=False`` reproduces the *old* parser shape (``content`` is just the
    heading, no ``title`` / ``body`` keys) so the generator is proven to work
    against both sides of the parser contract change.
    """
    sop = SOPContent()
    sop.title = title
    sop.version = version
    sop.purpose = _PURPOSE
    sop.scope = _SCOPE
    sop.responsibilities = list(_RESPONSIBILITIES)
    sop.safety_warnings = _WARNINGS[:n_warnings]
    sop.definitions = dict(_DEFINITIONS[:n_definitions])

    procedures = []
    for index in range(n_steps):
        heading, body = _STEPS[index % len(_STEPS)]
        number = step_numbers[index] if step_numbers else str(index + 1)
        if with_body:
            procedures.append({
                "step_number": number,
                "title": heading,
                "body": body,
                "content": heading + "\n" + body,
                "substeps": [],
                "source_lines": [index * 4, index * 4 + 3],
            })
        else:
            procedures.append({
                "step_number": number,
                "content": heading,
                "substeps": [],
            })
    sop.procedures = procedures
    return sop


@functools.lru_cache(maxsize=None)
def get_document(name):
    """Documents under test, cached (generation never mutates them)."""
    if name == "sample":
        return SOPParser().parse(str(SAMPLE_SOP))
    if name == "numbered":
        # Thin, numbered-convention SOP: 3 steps (4.1-4.3), 2 warnings.
        return SOPParser().parse(str(NUMBERED_SOP))
    if name == "rich":
        # 9 steps with bodies, 5 warnings, 4 definitions
        return make_sop("Trim Blade Replacement SOP", "3.2", 9, 5, 4)
    if name == "sparse":
        # 3 steps, 1 warning, no definitions - the low-material end
        return make_sop("Nitrogen Purge Check SOP", "1.0", 3, 1, 0)
    if name == "headings_only":
        # Old parser shape: 6 headings, no bodies, 3 warnings, 2 definitions
        return make_sop("Legacy Parse SOP", "2.0", 6, 3, 2, with_body=False)
    if name == "dotted":
        # Numbered-convention SOP: 4.1 .. 4.11, so "4.10" must sort after "4.9"
        numbers = ["4.{0}".format(i) for i in range(1, 12)]
        return make_sop("Numbered Convention SOP", "5.1", 11, 4, 3,
                        step_numbers=numbers)
    if name in GALLERY_SAMPLES:
        # Real, realistic SOPs from six regulatory regimes (examples/gallery/),
        # parsed through the real parser -- not hand-built like make_sop().
        return SOPParser().parse(str(GALLERY_SAMPLES[name]))
    raise KeyError(name)


DOCUMENTS = ("sample", "numbered", "rich", "sparse", "headings_only") + tuple(GALLERY_SAMPLES)
GENERATORS = ("base", "medical_device")


def make_generator(name):
    return AssessmentGenerator() if name == "base" else MedicalDeviceAssessmentGenerator()


@functools.lru_cache(maxsize=None)
def get_assessment(doc_name, generator_name, num_questions):
    return make_generator(generator_name).generate(
        get_document(doc_name), num_questions=num_questions)


# ---------------------------------------------------------------------------
# Naive-learner strategies
#
# Every way a learner can answer the whole quiz without reading the SOP.  The
# index strategies appear TWICE, once per realistic fallback: "always click the
# third option" is undefined on a two-option question, and a learner faced with
# one still clicks something.  Modelling that as "collects nothing" is how a
# three-question quiz on examples/sample_sop_numbered.txt once let "always
# option 3" score 100% - its smallest question had two options.
# ---------------------------------------------------------------------------
def _pick_index(aim, fallback):
    def picker(question):
        count = len(question.options)
        if aim < count:
            return aim
        return 0 if fallback == "first" else count - 1
    return picker


def _pick_last(question):
    return len(question.options) - 1


def _pick_true(question):
    return question.options.index("True") if "True" in question.options else -1


def _pick_longest(question):
    return max(range(len(question.options)),
               key=lambda i: (len(question.options[i]), -i))


def _pick_shortest(question):
    return min(range(len(question.options)),
               key=lambda i: (len(question.options[i]), i))


NAIVE_STRATEGIES = {
    "always_last_option": _pick_last,
    "always_true": _pick_true,
    "always_longest_option": _pick_longest,
    "always_shortest_option": _pick_shortest,
}
for _aim in range(MAX_OPTIONS):
    for _fallback in ("first", "last"):
        NAIVE_STRATEGIES["always_option_{0}_else_{1}".format(_aim + 1, _fallback)] = (
            _pick_index(_aim, _fallback))


def naive_score(assessment, picker):
    """Score a learner who answers without reading, using the Python model's
    post-shuffle ``correct_answer``."""
    total = sum(q.points for q in assessment.questions)
    if not total:
        return 0.0
    earned = sum(q.points for q in assessment.questions
                 if picker(q) == q.correct_answer)
    return 100.0 * earned / total


# ---------------------------------------------------------------------------
# Defect 1: the quiz could be passed by always clicking the first option
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("num_questions", QUESTION_COUNTS)
@pytest.mark.parametrize("generator_name", GENERATORS)
@pytest.mark.parametrize("doc_name", DOCUMENTS)
def test_naive_learner_fails(doc_name, generator_name, num_questions):
    """A learner who never reads the SOP scores below the passing mark - whatever
    fixed strategy they use, on every document, at every supported length.

    This is the M0 exit criterion.  Guards: hardcoded correct_answer=0, options
    rendered in source order, the unconsumed randomize_options flag, "always
    True" on true/false items, and the length tell that made the correct option
    systematically the longest one.
    """
    assessment = get_assessment(doc_name, generator_name, num_questions)
    assert assessment.questions, "generator produced no questions"
    passing = assessment.passing_score

    failures = []
    for name in sorted(NAIVE_STRATEGIES):
        score = naive_score(assessment, NAIVE_STRATEGIES[name])
        if score >= passing:
            failures.append("{0} scored {1:.1f}%".format(name, score))

    assert not failures, (
        "on {0}/{1}/n={2} (passing {3}%, {4} questions) these strategies reach "
        "the passing mark without reading the SOP: {5}".format(
            doc_name, generator_name, num_questions, passing,
            len(assessment.questions), "; ".join(failures))
    )


def test_naive_learner_fails_on_the_shipped_fixtures():
    """Explicit sweep of both bundled SOPs through the real parser.

    Mirrors the checker's independent simulation, so a regression here is
    directly comparable to the numbers in its report.
    """
    worst = {}
    for doc_name in ("sample", "numbered"):
        for generator_name in GENERATORS:
            for num_questions in QUESTION_COUNTS:
                assessment = get_assessment(doc_name, generator_name, num_questions)
                for name, picker in NAIVE_STRATEGIES.items():
                    score = naive_score(assessment, picker)
                    assert score < assessment.passing_score, (
                        "{0} scored {1:.1f}% (passing {2}%) on {3}/{4}/n={5}".format(
                            name, score, assessment.passing_score, doc_name,
                            generator_name, num_questions))
                    worst[name] = max(worst.get(name, 0.0), score)
    # Nothing should even be close; leave headroom below the lowest passing mark.
    assert max(worst.values()) < 70.0, worst


def test_naive_learner_fails_medical_device_passing_score():
    """Medical device training passes at 80%; every naive strategy must miss it."""
    assessment = get_assessment("sample", "medical_device", 8)
    assert assessment.passing_score >= 80
    for name, picker in NAIVE_STRATEGIES.items():
        assert naive_score(assessment, picker) < assessment.passing_score, name


# ---------------------------------------------------------------------------
# Option length must not be a tell
# ---------------------------------------------------------------------------
def test_clip_option_never_exceeds_its_limit():
    """The ellipsis is counted against the budget, not added on top.

    When it was added on top, clipped options came out at 176-183 characters
    instead of a flat cap, and the jitter made the correct answer the longest
    option on nearly every step question.
    """
    long_text = " ".join("word{0}".format(i) for i in range(200))
    for limit in (20, 40, 100, MAX_OPTION_CHARS):
        clipped = clip_option(long_text, limit)
        assert len(clipped) <= limit, (limit, len(clipped))
        assert clipped.endswith("...")
    # Short text is returned untouched.
    assert clip_option("Close the valve.") == "Close the valve."
    assert clip_option("  Close   the\n valve. ") == "Close the valve."


def _questions_with_three_or_more_options():
    questions = []
    for doc_name in DOCUMENTS:
        for generator_name in GENERATORS:
            for num_questions in QUESTION_COUNTS:
                assessment = get_assessment(doc_name, generator_name, num_questions)
                questions.extend(q for q in assessment.questions
                                 if len(q.options) >= 3)
    return questions


def test_correct_option_is_not_systematically_longest_or_shortest():
    """Length must carry no signal.

    Distractors are chosen straddling the correct answer's length, so the
    correct option's length rank is about 1/k - the same as chance.
    """
    questions = _questions_with_three_or_more_options()
    assert len(questions) >= 40

    longest = shortest = 0
    for question in questions:
        lengths = [len(o) for o in question.options]
        if lengths[question.correct_answer] == max(lengths):
            longest += 1
        if lengths[question.correct_answer] == min(lengths):
            shortest += 1

    longest_share = 100.0 * longest / len(questions)
    shortest_share = 100.0 * shortest / len(questions)
    assert longest_share <= 40.0, (
        "correct option is the longest in {0:.1f}% of {1} questions".format(
            longest_share, len(questions)))
    assert shortest_share <= 40.0, (
        "correct option is the shortest in {0:.1f}% of {1} questions".format(
            shortest_share, len(questions)))


def _distinct_text(index, length):
    """Text of exactly `length` characters sharing no vocabulary with the others.

    Synthetic on purpose: this exercises the length-banding arithmetic in
    isolation, so the near-identical filter must not be the thing under test.
    """
    from random import Random as _Random

    rng = _Random(index * 9176 + 3)
    words = ["t{0}w{1}".format(index, rng.randrange(10 ** 6)) for _ in range(60)]
    return " ".join(words)[:length].rstrip()


def test_pick_distractors_straddles_the_correct_answer_length():
    """The mechanism, tested directly on a pool with material on both sides.

    Taking simply the closest candidates by absolute length difference put every
    distractor on one side of the correct answer whenever the correct answer sat
    near an end of the pool's length distribution, which is what made "always the
    longest option" a winning strategy.
    """
    from random import Random as _Random

    correct = _distinct_text(0, 100)
    pool = [_distinct_text(i, n) for i, n in
            enumerate((40, 55, 70, 85, 115, 130, 145, 160), start=1)]

    chosen = pick_distractors(correct, pool, _Random(12345))
    assert len(chosen) == 3
    lengths = [len(c) for c in chosen]
    assert any(n > len(correct) for n in lengths), lengths
    assert any(n < len(correct) for n in lengths), lengths
    # Deterministic for a given seed.
    assert pick_distractors(correct, pool, _Random(12345)) == chosen


def test_pick_distractors_copes_when_only_one_side_has_material():
    """No longer candidates: take the closest shorter ones rather than failing."""
    from random import Random as _Random

    correct = _distinct_text(0, 160)
    pool = [_distinct_text(i, n) for i, n in enumerate((40, 60, 80, 100), start=1)]
    chosen = pick_distractors(correct, pool, _Random(7))
    assert len(chosen) == 3
    assert all(len(c) < len(correct) for c in chosen)


def test_true_false_items_are_worth_less_than_multiple_choice():
    """Points follow the guess baseline: 50% for two options, 25% for four.

    Crediting a true/false item the same as a four-option item over-rewards
    guessing.  It also concentrated points on questions where "False" - always
    the longer option - was the answer.
    """
    assert POINTS_TRUE_FALSE * 2 == POINTS_CHOICE
    assert POINTS_SAFETY_TRUE_FALSE * 2 == POINTS_SAFETY_CHOICE
    assert POINTS_SAFETY_CHOICE > POINTS_CHOICE

    assessment = get_assessment("sample", "base", 12)
    for question in assessment.questions:
        if len(question.options) <= 2:
            # Two options means a 50% guess baseline, whatever the question was
            # authored as - a multiple-choice question the document could only
            # give two options is weighted like a true/false one.
            assert question.points <= POINTS_SAFETY_TRUE_FALSE
        else:
            assert question.points >= POINTS_CHOICE

    assert scale_points_for_options(POINTS_SAFETY_CHOICE, 2) == POINTS_SAFETY_TRUE_FALSE
    assert scale_points_for_options(POINTS_SAFETY_CHOICE, 4) == POINTS_SAFETY_CHOICE
    assert scale_points_for_options(POINTS_CHOICE, 3) == POINTS_CHOICE
    assert scale_points_for_options(1, 2) == 1  # never zero

    # No single question may dominate a minimum-length assessment.
    for doc_name in DOCUMENTS:
        short = get_assessment(doc_name, "base", MIN_ASSESSMENT_QUESTIONS)
        total = sum(q.points for q in short.questions)
        assert max(q.points for q in short.questions) <= 0.5 * total, doc_name


def test_length_rebalance_preserves_category_coverage():
    """Swapping a length-extreme question only ever uses the same category, so
    the coverage blueprint is unaffected."""
    from collections import Counter

    from src.answer_key import document_key

    for doc_name in DOCUMENTS:
        for num_questions in QUESTION_COUNTS:
            generator = AssessmentGenerator()
            document = get_document(doc_name)
            doc_key = document_key(document.title or "", document.version or "")
            pool = generator._build_pool(document, doc_key)
            selected = generator._select_by_blueprint(pool, num_questions)
            before = Counter(c.category for c in selected)
            after = Counter(c.category for c in
                            generator._rebalance_length_extremes(list(selected), pool))
            assert before == after, (doc_name, num_questions, before, after)


def test_length_rebalance_never_duplicates_a_question():
    for doc_name in DOCUMENTS:
        for generator_name in GENERATORS:
            for num_questions in QUESTION_COUNTS:
                assessment = get_assessment(doc_name, generator_name, num_questions)
                ids = [q.id for q in assessment.questions]
                assert len(set(ids)) == len(ids), (doc_name, num_questions, ids)


# ---------------------------------------------------------------------------
# Minimum assessment length
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("num_questions", BELOW_MINIMUM_COUNTS)
@pytest.mark.parametrize("generator_name", GENERATORS)
def test_short_requests_are_raised_to_the_minimum(generator_name, num_questions):
    """Below the floor the arithmetic cannot work, so the count is raised and
    the caller is told - rather than shipping a quiz that can be gamed.

    A three-question quiz in which one question carries two of four points has
    no layout that keeps every fixed strategy under the passing mark.
    """
    assessment = make_generator(generator_name).generate(
        get_document("numbered"), num_questions=num_questions)

    assert assessment.requested_questions == num_questions
    assert len(assessment.questions) >= MIN_ASSESSMENT_QUESTIONS
    assert assessment.notes, "raising the count must be recorded"
    assert str(MIN_ASSESSMENT_QUESTIONS) in assessment.notes[0]
    assert "notes" in assessment.to_dict()

    for name, picker in NAIVE_STRATEGIES.items():
        assert naive_score(assessment, picker) < assessment.passing_score, name


def test_minimum_is_enforced_by_the_cli_and_the_web_api():
    """The floor is not just a silent clamp - the entry points reject below it."""
    import app as flask_app
    from src.cli import main as cli_main

    assert flask_app.MIN_ASSESSMENT_QUESTIONS == MIN_ASSESSMENT_QUESTIONS
    questions_option = next(p for p in cli_main.params if p.name == "questions")
    assert questions_option.type.min == MIN_ASSESSMENT_QUESTIONS


# ---------------------------------------------------------------------------
# Answer position distribution
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def _wide_question_sample():
    """Four-option questions across several documents, versions and counts."""
    questions = []
    documents = [get_document(name) for name in DOCUMENTS]
    # Extra revisions of the synthetic document: a different version string is a
    # different document key, hence a different seed.
    for version in ("1.0", "2.3", "7.11"):
        documents.append(make_sop("Trim Blade Replacement SOP", version, 9, 5, 4))

    for document in documents:
        for generator_name in GENERATORS:
            for num_questions in (5, 8, 12):
                assessment = make_generator(generator_name).generate(
                    document, num_questions=num_questions)
                questions.extend(q for q in assessment.questions
                                 if len(q.options) == 4)
    return tuple(questions)


def test_answer_position_is_not_concentrated():
    """No option position may hold more than 45% of the correct answers."""
    questions = _wide_question_sample()
    assert len(questions) >= 40, "need >= 40 four-option questions to judge"

    counts = Counter(q.correct_answer for q in questions)
    for position in range(4):
        share = 100.0 * counts[position] / len(questions)
        assert share <= 45.0, (
            "position {0} holds {1:.1f}% of correct answers across {2} "
            "four-option questions".format(position, share, len(questions))
        )
    assert set(counts) == {0, 1, 2, 3}, "every position must be used"


# ---------------------------------------------------------------------------
# Defect 4: non-deterministic selection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("num_questions", QUESTION_COUNTS)
@pytest.mark.parametrize("generator_name", GENERATORS)
@pytest.mark.parametrize("doc_name", DOCUMENTS)
def test_generation_is_deterministic(doc_name, generator_name, num_questions):
    """Same input, same package. Reproducible builds are part of validation."""
    document = get_document(doc_name)
    first = make_generator(generator_name).generate(
        document, num_questions=num_questions).to_dict()
    second = make_generator(generator_name).generate(
        document, num_questions=num_questions).to_dict()
    assert first == second


def test_different_revisions_shuffle_differently():
    """A new SOP revision is a new document key, so the layout moves."""
    v1 = AssessmentGenerator().generate(
        make_sop("Trim Blade Replacement SOP", "1.0", 9, 5, 4), num_questions=8)
    v2 = AssessmentGenerator().generate(
        make_sop("Trim Blade Replacement SOP", "2.0", 9, 5, 4), num_questions=8)
    assert [q.correct_answer for q in v1.questions] != [q.correct_answer for q in v2.questions]


# ---------------------------------------------------------------------------
# Defect 3: distractors were giveaways
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("num_questions", QUESTION_COUNTS)
@pytest.mark.parametrize("generator_name", GENERATORS)
@pytest.mark.parametrize("doc_name", DOCUMENTS)
def test_options_are_distinct_plausible_and_non_empty(doc_name, generator_name,
                                                      num_questions):
    from src.answer_key import normalize_option_text

    assessment = get_assessment(doc_name, generator_name, num_questions)
    for question in assessment.questions:
        assert 2 <= len(question.options) <= 4, question.id

        normalised = [normalize_option_text(o) for o in question.options]
        assert all(normalised), "empty option in {0}".format(question.id)
        assert len(set(normalised)) == len(normalised), (
            "duplicate option in {0}".format(question.id))

        correct = normalised[question.correct_answer]
        for index, option in enumerate(normalised):
            if index == question.correct_answer:
                continue
            assert option != correct, "distractor equals correct answer"
            if question.type != "true_false":
                assert option not in correct and correct not in option, (
                    "option {0!r} gives away the answer in {1}".format(
                        question.options[index], question.id))

        for option in question.options:
            for filler in ABSURD_FILLERS:
                assert filler.lower() not in option.lower(), (
                    "pre-M0 filler {0!r} reappeared in {1}".format(filler, question.id))


def test_safety_distractors_are_altered_warnings_from_the_document():
    """Safety wrong answers come from the SOP's own warnings, flipped."""
    document = get_document("rich")
    assessment = AssessmentGenerator().generate(document, num_questions=12)
    safety = [q for q in assessment.questions
              if q.source_ref.get("kind") in ("safety", "safety_contradiction")
              and q.type != "true_false"]
    assert safety, "expected safety multiple-choice questions"

    real = {w.rstrip(".").lower() for w in document.safety_warnings}
    altered = {alter_statement(w).rstrip(".").lower()
               for w in document.safety_warnings if alter_statement(w)}
    for question in safety:
        for option in question.options:
            key = option.rstrip(".").lower()
            assert key in real or key in altered, (
                "safety option not traceable to a document warning: {0!r}".format(option))


def test_alter_statement_flips_obligations():
    assert alter_statement("Never restart the line.").startswith("Always")
    assert "may optionally" in alter_statement("All personnel must evacuate the area.")
    assert alter_statement("Bleed the line before any contact.").endswith(
        "after any contact.")
    # No honest flip available -> None, never invented filler.
    assert alter_statement("Record the serial number on the log sheet.") is None


# ---------------------------------------------------------------------------
# Coverage blueprint
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("num_questions", QUESTION_COUNTS)
@pytest.mark.parametrize("generator_name", GENERATORS)
@pytest.mark.parametrize("doc_name", DOCUMENTS)
def test_safety_and_sequence_coverage(doc_name, generator_name, num_questions):
    """Blueprint priority: medical-device required questions, then >=1 safety
    question, then >=1 sequence question.

    Each guarantee only applies when the requested count leaves room for it -
    a three-question medical-device quiz legitimately spends its whole budget on
    the two compliance questions plus one safety question, because the count is
    honoured exactly.
    """
    document = get_document(doc_name)
    assessment = get_assessment(doc_name, generator_name, num_questions)
    kinds = {q.source_ref.get("kind") for q in assessment.questions}

    reserved = sum(1 for q in assessment.questions
                   if q.source_ref.get("kind") == "md_required")
    if generator_name == "medical_device":
        assert reserved == min(2, num_questions), "required questions crowded out"

    if document.safety_warnings and num_questions > reserved:
        assert kinds & {"safety", "safety_contradiction"}, (
            "warnings exist but no safety question was included")
        reserved += 1

    if len(document.procedures) >= 3 and num_questions > reserved:
        assert "sequence" in kinds, "3+ steps but no step-sequence question"


def test_blueprint_priority_order_on_a_tight_budget():
    """The scarcest budget still buys the highest-priority coverage first."""
    document = get_document("rich")

    base = AssessmentGenerator().generate(
        document, num_questions=MIN_ASSESSMENT_QUESTIONS)
    kinds = [q.source_ref.get("kind") for q in base.questions]
    assert len(base.questions) == MIN_ASSESSMENT_QUESTIONS
    assert sum(1 for k in kinds if k in ("safety", "safety_contradiction")) >= 1
    assert kinds.count("sequence") >= 1

    medical = MedicalDeviceAssessmentGenerator().generate(
        document, num_questions=MIN_ASSESSMENT_QUESTIONS)
    ids = {q.id for q in medical.questions}
    assert {"md_req_1", "md_req_2"} <= ids
    assert len(medical.questions) == MIN_ASSESSMENT_QUESTIONS


@pytest.mark.parametrize("num_questions", (5, 8))
def test_question_count_is_honoured_exactly_on_the_sample_sop(num_questions):
    for generator_name in GENERATORS:
        assessment = get_assessment("sample", generator_name, num_questions)
        assert len(assessment.questions) == num_questions
        assert assessment.requested_questions == num_questions


def test_requested_versus_available_is_exposed():
    """A document that cannot support the ask returns the whole pool and says so."""
    tiny = make_sop("Tiny SOP", "1.0", 2, 1, 0)
    assessment = AssessmentGenerator().generate(tiny, num_questions=40)
    assert assessment.requested_questions == 40
    assert len(assessment.questions) < 40
    assert assessment.questions


def test_step_questions_are_spread_across_the_procedure():
    """Defect: short quizzes used to cluster on whichever steps random.sample hit."""
    assessment = AssessmentGenerator().generate(get_document("rich"), num_questions=12)
    steps = [int(q.source_ref["step_number"]) for q in assessment.questions
             if q.source_ref.get("kind") == "step"]
    assert len(steps) >= 3
    assert len(set(steps)) == len(steps), "the same step was asked about twice"
    assert max(steps) - min(steps) >= 4, "step questions cluster in one part of the SOP"


# ---------------------------------------------------------------------------
# Defect 5: the ordering question type was unanswerable
# ---------------------------------------------------------------------------
def test_ordering_question_type_is_gone():
    for doc_name in DOCUMENTS:
        for generator_name in GENERATORS:
            for num_questions in QUESTION_COUNTS:
                assessment = get_assessment(doc_name, generator_name, num_questions)
                for question in assessment.questions:
                    assert question.type != "ordering"
                    assert isinstance(question.correct_answer, int)
                    assert 0 <= question.correct_answer < len(question.options)


@pytest.mark.parametrize("doc_name", ("sample", "rich", "dotted"))
def test_sequence_answer_is_the_next_step(doc_name):
    from src.answer_key import normalize_option_text
    from src.assessments import step_number, step_title

    document = get_document(doc_name)
    assessment = AssessmentGenerator().generate(document, num_questions=12)
    steps = ordered_steps(document.procedures)
    by_number = {step_number(s): s for s in steps}
    order = [step_number(s) for s in steps]

    sequence_questions = [q for q in assessment.questions if q.type == "sequence"]
    assert sequence_questions, "no sequence question generated"

    for question in sequence_questions:
        after = question.source_ref["after_step"]
        answer_step = question.source_ref["answer_step"]
        # The recorded answer step really is the successor in execution order.
        assert order[order.index(after) + 1] == answer_step

        expected = normalize_option_text(step_title(by_number[answer_step]))
        actual = normalize_option_text(question.options[question.correct_answer])
        assert actual == expected, (
            "correct option {0!r} is not the next step's title".format(
                question.options[question.correct_answer]))

        # The step number must not appear in the options, or the answer could be
        # read straight off the labels.
        for option in question.options:
            assert not re.match(r"^\s*step\s+\S+\s*:", option, re.IGNORECASE)


def test_dotted_step_numbers_sort_naturally():
    """4.10 comes after 4.9, not next to 4.1."""
    numbers = ["4.1", "4.10", "4.2", "4.9", "4.11", "4.3"]
    procedures = [{"step_number": n, "content": "Step body for " + n} for n in numbers]
    assert [p["step_number"] for p in ordered_steps(procedures)] == [
        "4.1", "4.2", "4.3", "4.9", "4.10", "4.11"]


def test_step_sort_key_tolerates_non_numeric_step_numbers():
    """Unparseable step numbers keep document order instead of raising.

    The pre-M0 code did int("4.3") and died with ValueError.
    """
    procedures = [
        {"step_number": "Appendix A", "content": "a"},
        {"step_number": "2", "content": "b"},
        {"step_number": "", "content": "c"},
        {"step_number": "1", "content": "d"},
    ]
    result = [p["step_number"] for p in ordered_steps(procedures)]
    assert result[:2] == ["1", "2"]          # numbered steps first, in order
    assert result[2:] == ["Appendix A", ""]  # the rest keep document order
    assert step_sort_key({"step_number": "4.10"}, 0) > step_sort_key(
        {"step_number": "4.9"}, 1)


def test_sequence_questions_survive_dotted_numbering():
    document = get_document("dotted")
    assessment = AssessmentGenerator().generate(document, num_questions=12)
    sequence = [q for q in assessment.questions if q.type == "sequence"]
    assert sequence
    for question in sequence:
        after = question.source_ref["after_step"]
        answer = question.source_ref["answer_step"]
        major_a, minor_a = (int(p) for p in after.split("."))
        major_b, minor_b = (int(p) for p in answer.split("."))
        assert major_a == major_b and minor_b == minor_a + 1


# ---------------------------------------------------------------------------
# True/false balance
# ---------------------------------------------------------------------------
def test_true_false_statements_are_roughly_half_false():
    """"Always True" has to fail, so most quizzes must contain false statements."""
    seen_false = 0
    seen_total = 0
    for doc_name in DOCUMENTS:
        for num_questions in (8, 12):
            assessment = get_assessment(doc_name, "base", num_questions)
            true_false = [q for q in assessment.questions if q.type == "true_false"]
            if len(true_false) < 2:
                continue
            false_count = sum(1 for q in true_false if q.correct_answer == 1)
            assert false_count >= len(true_false) // 2, (
                "{0}/n={1}: only {2} of {3} true/false items are false".format(
                    doc_name, num_questions, false_count, len(true_false)))
            seen_false += false_count
            seen_total += len(true_false)
    assert seen_total and seen_false


# ---------------------------------------------------------------------------
# Defect 6: MedicalDeviceAssessmentGenerator did not honour the count
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("num_questions", QUESTION_COUNTS)
def test_medical_device_required_questions_and_count(num_questions):
    assessment = get_assessment("sample", "medical_device", num_questions)

    assert len(assessment.questions) == num_questions
    assert assessment.passing_score >= 80

    ids = {q.id for q in assessment.questions}
    assert {"md_req_1", "md_req_2"} <= ids, "required compliance questions missing"

    texts = " ".join(q.text.lower() for q in assessment.questions)
    assert "cannot follow this procedure as written" in texts
    assert "product quality or patient safety" in texts


def test_medical_device_required_questions_are_shuffled_too():
    """The compliance questions must not be a free 100% either."""
    positions = set()
    for version in ("1.0", "2.0", "3.0", "4.0", "5.0", "6.0"):
        document = make_sop("Trim Blade Replacement SOP", version, 9, 5, 4)
        assessment = MedicalDeviceAssessmentGenerator().generate(
            document, num_questions=8)
        question = next(q for q in assessment.questions if q.id == "md_req_1")
        positions.add(question.correct_answer)
    assert len(positions) > 1, "md_req_1 always lands on the same option"


def test_medical_device_passing_score_floor():
    assessment = MedicalDeviceAssessmentGenerator().generate(
        get_document("sample"), num_questions=5, passing_score=50)
    assert assessment.passing_score == 80


# ---------------------------------------------------------------------------
# Defect 2: the learner payload must not carry the key
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("generator_name", GENERATORS)
def test_learner_dict_strips_the_answer_key(generator_name):
    from src.answer_key import answer_hash

    assessment = get_assessment("sample", generator_name, 8)
    payload = assessment.to_learner_dict()

    assert payload == assessment.to_client_payload()
    assert "questions" in payload
    assert len(payload["questions"]) == len(assessment.questions)

    for question, shipped in zip(assessment.questions, payload["questions"]):
        assert set(shipped) == {"id", "type", "text", "options", "points",
                                "salt", "answer_hash", "topic"}
        assert shipped["options"] == question.options
        assert len(shipped["salt"]) == 32
        assert re.fullmatch(r"[0-9a-f]{64}", shipped["answer_hash"])
        # The hash really is of the correct option and of nothing else.
        assert shipped["answer_hash"] == answer_hash(
            question.salt, question.options[question.correct_answer])
        for index, option in enumerate(question.options):
            if index != question.correct_answer:
                assert answer_hash(question.salt, option) != shipped["answer_hash"]


def test_author_dict_keeps_the_answers_for_sme_review():
    """to_dict() is the SME/author export and MAY contain answers."""
    assessment = get_assessment("sample", "base", 5)
    data = assessment.to_dict()
    for key in ("title", "description", "questions", "passing_score", "time_limit",
                "randomize_questions", "randomize_options"):
        assert key in data, "backward-compatible key {0} disappeared".format(key)
    for question in data["questions"]:
        for key in ("id", "type", "text", "options", "correct_answer",
                    "explanation", "points"):
            assert key in question


def test_salts_are_unique_per_question():
    assessment = get_assessment("sample", "medical_device", 12)
    salts = [q.salt for q in assessment.questions]
    assert len(set(salts)) == len(salts)


# ---------------------------------------------------------------------------
# Parser contract tolerance
# ---------------------------------------------------------------------------
def test_full_step_body_is_used_when_the_parser_provides_it():
    document = get_document("rich")
    assessment = AssessmentGenerator().generate(document, num_questions=12)
    step_questions = [q for q in assessment.questions
                      if q.source_ref.get("kind") == "step"]
    assert step_questions

    bodies = {p["body"][:40] for p in document.procedures}
    hits = sum(1 for q in step_questions
               if any(q.options[q.correct_answer].startswith(b) for b in bodies))
    assert hits == len(step_questions), "step answers should be the step bodies"
    # source_lines travel through for traceability
    assert all(q.source_ref.get("source_lines") for q in step_questions)


def test_heading_only_parse_does_not_leak_the_answer_into_the_prompt():
    """With the old parser the heading IS the answer, so it must not be quoted
    in the question text."""
    document = get_document("headings_only")
    assessment = AssessmentGenerator().generate(document, num_questions=12)
    for question in assessment.questions:
        if question.source_ref.get("kind") != "step":
            continue
        answer = question.options[question.correct_answer]
        assert answer.lower() not in question.text.lower()


def test_generator_handles_an_empty_document():
    empty = SOPContent()
    empty.title = "Empty"
    assessment = AssessmentGenerator().generate(empty, num_questions=5)
    assert assessment.questions == []
    assert assessment.requested_questions == 5
    assert assessment.to_learner_dict()["questions"] == []
