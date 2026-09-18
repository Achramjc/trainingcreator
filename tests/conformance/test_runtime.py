"""SCORM run-time conformance, driven in a real browser against a fake LMS.

The package's own pages are loaded two frames below a host window that
publishes ``API`` (SCORM 1.2) or ``API_1484_11`` (SCORM 2004).  Nothing is
injected into the SCO: it has to discover the API by walking its ancestors,
which is what an LMS makes it do and what the exporter's wrapper used to get
wrong for 2004.

Everything is served over ``http://127.0.0.1`` so relative asset paths behave
as they do in a deployed package.  The browser tests skip cleanly when no
Chromium is installed, so the plain CI job stays green.
"""

import json
import math
import re
import shutil

import pytest

from tests.conformance import fake_lms


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class Session:
    """One page of one package, running inside the fake LMS."""

    def __init__(self, page, base_url, version):
        self.page = page
        self.base_url = base_url
        self.version = version
        self.errors = []

    @property
    def sco(self):
        """The frame the SCO is in (host -> shell -> sco)."""
        if self.version is None:
            return self.page.frame_locator("#sco")
        return self.page.frame_locator("#shell").frame_locator("#sco")

    def recording(self):
        return fake_lms.read_recording(self.page)


def _open(browser, tmp_path, package, page_name, version):
    """Copy *package* under a served root, host it, and return a Session.

    ``version`` is the API surface the fake LMS exposes, or ``None`` for the
    no-LMS case.  It is passed separately from ``package.version`` so a 1.2
    package can be pointed at a 2004 LMS and vice versa.
    """
    root = tmp_path / "site"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package.dir, root / "package", dirs_exist_ok=True)
    entry = fake_lms.write_host(root, "package/" + page_name, version=version)

    context = browser.new_context()
    page = context.new_page()
    session = Session(page, None, version)
    page.on("pageerror", lambda exc: session.errors.append(str(exc)))
    page.on("dialog", lambda dialog: dialog.accept())

    with fake_lms.serve(root) as base_url:
        session.base_url = base_url
        page.goto("{0}/{1}".format(base_url, entry))
        # Both frames have to be up before the SCO can have found anything.
        session.sco.locator("body").wait_for(timeout=15000)
        yield session

    context.close()


@pytest.fixture
def open_sco(chromium, tmp_path):
    """``open_sco(package, page_name, version)`` -> Session, as a context."""
    import contextlib

    @contextlib.contextmanager
    def factory(package, page_name="assessment.html", version="unset"):
        api_version = package.version if version == "unset" else version
        generator = _open(chromium, tmp_path, package, page_name, api_version)
        session = next(generator)
        try:
            yield session
        finally:
            for _ in generator:
                pass

    return factory


def _answer(session, package, correct, skip=()):
    """Play one strategy through the assessment and submit.

    ``skip`` is a set of question ids to leave blank, which is how the
    "learner never answered this one" branch of the interaction record is
    reached.
    """
    for question in package.assessment.questions:
        if question.id in skip:
            continue
        index = question.correct_answer if correct else 0
        session.sco.locator(
            'input[name="q_{0}"]'.format(question.id)).nth(index).check()
    session.sco.locator("#submit-button").click()


def _wait_for_result(session, outcome):
    session.sco.locator("#results.results.{0}".format(outcome)).wait_for(
        timeout=20000)


# ---------------------------------------------------------------------------
# Shared properties, both API surfaces
# ---------------------------------------------------------------------------
@pytest.fixture(params=["1.2", "2004"])
def scored(request, package_factory, open_sco):
    """A passing attempt on a package of the requested version."""
    package = package_factory(request.param)
    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        yield package, session, session.recording()


def test_the_sco_finds_the_api_two_frames_up(scored):
    package, session, recording = scored
    assert recording.calls, (
        "the SCO made no API calls at all: it never found the LMS.\n"
        "This is what a {0} package does inside a {0} LMS when its wrapper "
        "looks for the other version's API object.".format(package.version))
    assert not session.errors, session.errors


def test_initialize_happens_once_and_first(scored):
    package, _session, recording = scored
    init = "Initialize" if package.version == "2004" else "LMSInitialize"
    initializations = recording.of(init)
    assert len(initializations) == 1, recording.pretty()
    assert recording.names()[0] == init, recording.pretty()
    assert initializations[0]["args"] == [""], initializations[0]
    assert initializations[0]["ret"] == "true"

    first_set = recording.index_of("SetValue", "LMSSetValue")
    assert first_set is None or first_set > 0, recording.pretty()


def test_every_value_crossing_the_api_is_a_string(scored):
    _package, _session, recording = scored
    assert recording.sets()
    for call in recording.sets():
        assert call["argTypes"] == ["string", "string"], call


def test_commit_then_terminate_and_nothing_after(scored):
    package, _session, recording = scored
    commit = "Commit" if package.version == "2004" else "LMSCommit"
    finish = "Terminate" if package.version == "2004" else "LMSFinish"

    assert recording.of(commit), recording.pretty()
    finishes = recording.of(finish)
    assert len(finishes) == 1, recording.pretty()
    assert finishes[0]["args"] == [""]
    assert finishes[0]["ret"] == "true"

    finish_at = recording.index_of(finish)
    last_commit = recording.last_index_of(commit)
    assert last_commit < finish_at, (
        "the session was terminated before the last commit\n"
        + recording.pretty())

    last_set = recording.last_index_of("SetValue", "LMSSetValue")
    assert last_set < finish_at, (
        "a value was set after the session ended\n" + recording.pretty())
    assert recording.state["terminated"] is True


def test_no_api_call_is_ever_rejected(scored):
    """An LMS answering "false" means the SCO broke the call sequence."""
    _package, _session, recording = scored
    for call in recording.calls:
        if call["fn"].endswith(("GetValue", "GetLastError", "GetErrorString",
                                "GetDiagnostic")):
            continue
        assert call["ret"] == "true", (call, recording.pretty())


# ---------------------------------------------------------------------------
# SCORM 1.2 data model
# ---------------------------------------------------------------------------
def test_scorm12_failing_attempt(package_factory, open_sco):
    package = package_factory("1.2")
    assert package.naive_percentage < package.passing_score, (
        "fixture no longer exercises the failing branch")

    with open_sco(package) as session:
        _answer(session, package, correct=False)
        _wait_for_result(session, "fail")
        recording = session.recording()

    assert recording.model["cmi.core.lesson_status"] == "failed", \
        recording.pretty()
    assert recording.model["cmi.core.score.raw"] == str(package.naive_percentage)
    assert recording.model["cmi.core.score.min"] == "0"
    assert recording.model["cmi.core.score.max"] == "100"
    # The learner is not silently marked complete on a fail.
    assert recording.last_set("cmi.core.lesson_status") == "failed"
    assert not session.errors, session.errors


def test_scorm12_passing_attempt(package_factory, open_sco):
    package = package_factory("1.2")
    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    assert recording.model["cmi.core.lesson_status"] == "passed", \
        recording.pretty()
    assert recording.model["cmi.core.score.raw"] == \
        str(package.perfect_percentage) == "100"
    assert recording.model["cmi.core.score.min"] == "0"
    assert recording.model["cmi.core.score.max"] == "100"
    # "completed" must never overwrite the pass the learner earned.
    assert recording.last_set("cmi.core.lesson_status") == "passed"
    assert "cmi.success_status" not in recording.model, (
        "a SCORM 1.2 SCO must not write the 2004 data model")
    assert "cmi.score.scaled" not in recording.model


def test_scorm12_uses_only_cmi_core_elements(package_factory, open_sco):
    """A 1.2 SCO may write cmi.core.*, and the two elements 1.2 keeps outside
    it: cmi.suspend_data and cmi.interactions.  Nothing else."""
    package = package_factory("1.2")
    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    written = recording.written_keys()
    allowed_core = {
        "cmi.core.lesson_status", "cmi.core.score.raw",
        "cmi.core.score.min", "cmi.core.score.max",
        "cmi.core.session_time", "cmi.core.exit",
    }
    for element in written:
        assert (element in allowed_core
                or element == "cmi.suspend_data"
                or element.startswith("cmi.interactions.")), element
    # The 1.2 elements that must be there, and the 2004 spellings that must not.
    assert allowed_core <= written, allowed_core - written
    assert not any(element.startswith(("cmi.score.", "cmi.completion_",
                                       "cmi.success_", "cmi.session_time",
                                       "cmi.exit"))
                   for element in written), written


def test_scorm12_session_time_and_exit(package_factory, open_sco):
    package = package_factory("1.2")
    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    # CMITimespan: HHHH:MM:SS.SS
    session_time = recording.last_set("cmi.core.session_time")
    assert re.fullmatch(r"\d{4}:[0-5]\d:[0-5]\d\.\d{2}", session_time or ""), \
        session_time
    # "" is the 1.2 vocabulary for an ordinary end of session.
    assert recording.last_set("cmi.core.exit") == ""


# ---------------------------------------------------------------------------
# SCORM 2004 data model
# ---------------------------------------------------------------------------
def test_scorm2004_failing_attempt(package_factory, open_sco):
    package = package_factory("2004")
    with open_sco(package) as session:
        _answer(session, package, correct=False)
        _wait_for_result(session, "fail")
        recording = session.recording()

    assert recording.model["cmi.success_status"] == "failed", recording.pretty()
    assert recording.model["cmi.completion_status"] == "completed"
    assert recording.model["cmi.score.raw"] == str(package.naive_percentage)

    scaled = float(recording.model["cmi.score.scaled"])
    assert 0.0 <= scaled <= 1.0
    assert abs(scaled - package.naive_percentage / 100.0) < 1e-9, (
        "cmi.score.scaled disagrees with cmi.score.raw")
    assert not session.errors, session.errors


def test_scorm2004_passing_attempt(package_factory, open_sco):
    package = package_factory("2004")
    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    assert recording.model["cmi.success_status"] == "passed", recording.pretty()
    assert recording.model["cmi.completion_status"] == "completed"
    assert recording.model["cmi.score.raw"] == "100"
    assert recording.model["cmi.score.min"] == "0"
    assert recording.model["cmi.score.max"] == "100"
    assert float(recording.model["cmi.score.scaled"]) == 1.0
    assert "cmi.core.lesson_status" not in recording.model, (
        "a SCORM 2004 SCO must not write the 1.2 data model")


def test_scorm2004_scaled_clears_the_manifest_pass_mark(package_factory,
                                                        open_sco):
    """The manifest says minNormalizedMeasure; the run-time must be able to
    satisfy it with what it reports."""
    package = package_factory("2004")
    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    assert float(recording.model["cmi.score.scaled"]) >= \
        package.passing_score / 100.0


def test_scorm2004_uses_only_the_2004_data_model(package_factory, open_sco):
    package = package_factory("2004")
    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    written = recording.written_keys()
    allowed = {
        "cmi.completion_status", "cmi.success_status", "cmi.score.raw",
        "cmi.score.min", "cmi.score.max", "cmi.score.scaled",
        "cmi.session_time", "cmi.exit", "cmi.suspend_data",
    }
    for element in written:
        assert element in allowed or element.startswith("cmi.interactions."), \
            element
    assert allowed <= written, allowed - written
    assert not any(element.startswith("cmi.core.") for element in written)


def test_scorm2004_session_time_and_exit(package_factory, open_sco):
    package = package_factory("2004")
    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    # timeinterval(second,10,2): an ISO 8601 duration.
    session_time = recording.last_set("cmi.session_time")
    assert re.fullmatch(r"PT\d+H\d+M\d+\.\d{2}S", session_time or ""), session_time
    assert recording.last_set("cmi.exit") == "normal"


# ---------------------------------------------------------------------------
# Re-entry: a recorded pass survives reopening the SCO
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_relaunch_does_not_erase_a_recorded_pass(version, package_factory,
                                                 open_sco):
    """``initializeSCORM`` used to set "incomplete" unconditionally, which
    wipes a learner's pass the moment they reopen the module."""
    package = package_factory(version)
    status_element = ("cmi.completion_status" if version == "2004"
                      else "cmi.core.lesson_status")
    earned = "completed" if version == "2004" else "passed"

    with open_sco(package) as session:
        # Attempt one: pass it properly.  The SCO ends its own session.
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        first = session.recording()
        assert first.model[status_element] == earned, first.pretty()

        # Attempt two: the same LMS, the same persisted data model, a new
        # launch.  Only the service's session state resets - a real LMS keeps
        # what the learner earned.
        session.page.evaluate(
            "() => { window.__scormState.initialized = false;"
            " window.__scormState.terminated = false;"
            " window.__scormState.commits = 0;"
            " window.__scormCalls = []; }")
        session.sco.locator("body").evaluate("() => window.location.reload()")
        session.sco.locator("#submit-button").wait_for(timeout=20000)
        relaunch = session.recording()

    assert relaunch.calls, "the SCO did not talk to the LMS on relaunch"
    assert relaunch.model[status_element] == earned, relaunch.pretty()
    if version == "2004":
        assert relaunch.model["cmi.success_status"] == "passed", relaunch.pretty()
    assert not any(call["args"][1] == "incomplete"
                   for call in relaunch.sets()), relaunch.pretty()


# ---------------------------------------------------------------------------
# Content pages
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_mark_as_complete_on_a_content_page(version, package_factory, open_sco):
    package = package_factory(version)
    with open_sco(package, page_name="content_1.html") as session:
        session.sco.get_by_text("Mark as Complete").click()
        session.page.wait_for_timeout(500)
        recording = session.recording()

    if version == "2004":
        assert recording.model["cmi.completion_status"] == "completed", \
            recording.pretty()
        assert "cmi.core.lesson_status" not in recording.model
    else:
        assert recording.model["cmi.core.lesson_status"] == "completed", \
            recording.pretty()
        assert "cmi.completion_status" not in recording.model

    commit = "Commit" if version == "2004" else "LMSCommit"
    assert recording.of(commit)
    assert not session.errors, session.errors


@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_a_content_page_initializes_before_it_reports(version, package_factory,
                                                      open_sco):
    package = package_factory(version)
    init = "Initialize" if version == "2004" else "LMSInitialize"
    with open_sco(package, page_name="content_1.html") as session:
        session.page.wait_for_timeout(300)
        recording = session.recording()

    assert recording.names()[:1] == [init], recording.pretty()


# ---------------------------------------------------------------------------
# No LMS at all
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_scores_and_renders_with_no_lms_present(version, package_factory,
                                                open_sco):
    """A package opened outside an LMS must still score and show the result,
    and must not throw looking for an API that is not there."""
    package = package_factory(version)
    with open_sco(package, version=None) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        text = session.sco.locator("#results").inner_text()
        assert "You Passed" in text
        assert re.search(r"Your score:\s*100%", text), text
        assert session.page.evaluate("typeof window.API") == "undefined"
        assert session.page.evaluate("typeof window.API_1484_11") == "undefined"
        assert not session.errors, session.errors

    with open_sco(package, version=None) as session:
        _answer(session, package, correct=False)
        _wait_for_result(session, "fail")
        assert "Additional Study Required" in \
            session.sco.locator("#results").inner_text()
        assert not session.errors, session.errors


@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_mark_as_complete_is_harmless_with_no_lms(version, package_factory,
                                                  open_sco):
    package = package_factory(version)
    with open_sco(package, page_name="content_1.html", version=None) as session:
        session.sco.get_by_text("Mark as Complete").click()
        session.page.wait_for_timeout(300)
        assert not session.errors, session.errors


# ---------------------------------------------------------------------------
# The wrong LMS: a 1.2 package must not pretend in a 2004 host, and vice versa
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package_version,lms_version", [("1.2", "2004"),
                                                         ("2004", "1.2")])
def test_the_wrapper_speaks_whichever_api_it_finds(package_version,
                                                   lms_version,
                                                   package_factory, open_sco):
    """The wrapper is discovery-driven, not build-driven: the same
    ``scorm_api.js`` ships in both packages, so a package dropped into the
    other kind of LMS still reports against that LMS's data model instead of
    silently reporting nothing."""
    package = package_factory(package_version)
    with open_sco(package, version=lms_version) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    if lms_version == "2004":
        assert recording.model["cmi.success_status"] == "passed"
        assert float(recording.model["cmi.score.scaled"]) == 1.0
    else:
        assert recording.model["cmi.core.lesson_status"] == "passed"
        assert recording.model["cmi.core.score.raw"] == "100"
    assert not session.errors, session.errors


# ---------------------------------------------------------------------------
# cmi.interactions - per-question evidence in the LMS record
#
# Everything below is re-derived from the Python model rather than imported
# from src/scorm_exporter.py: a test that imports the mapping it is checking
# cannot fail when the mapping is wrong.  The format rules asserted here are
# the ones docs/SCORM_CONFORMANCE.md cites - a real LMS enforces them, the fake
# LMS records whatever it is handed.
# ---------------------------------------------------------------------------
OPTION_IDS = "abcdefghijklmnopqrstuvwxyz"

#: 1.2 CMITimespan / 2004 timeinterval, and 1.2 CMITime / 2004 ISO timestamp.
LATENCY_PATTERN = {
    "1.2": r"\d{4}:[0-5]\d:[0-5]\d\.\d{2}",
    "2004": r"PT\d+H\d+M\d+\.\d{2}S",
}
WHEN_ELEMENT = {"1.2": "time", "2004": "timestamp"}
WHEN_PATTERN = {
    "1.2": r"[0-2]\d:[0-5]\d:[0-5]\d",
    "2004": r"\d{4}-\d{2}-\d{2}T[0-2]\d:[0-5]\d:[0-5]\dZ",
}
RESPONSE_ELEMENT = {"1.2": "student_response", "2004": "learner_response"}
WRONG = {"1.2": "wrong", "2004": "incorrect"}

INTERACTION_MATRIX = [
    (version, generator, fixture, strategy)
    for version in ("1.2", "2004")
    for generator in ("standard", "medical")
    for fixture in ("sample", "numbered")
    for strategy in ("always-first", "all-correct")
]
INTERACTION_IDS = ["{0}-{1}-{2}-{3}".format(*case)
                   for case in INTERACTION_MATRIX]


def _expected_interaction_type(question):
    return "true-false" if question.type == "true_false" else "choice"


def _expected_response(question, chosen, version):
    """The response token the data model expects for *chosen* (an index)."""
    if question.type == "true_false":
        truthy = question.options[chosen].strip().lower() == "true"
        if version == "2004":
            return "true" if truthy else "false"
        return "t" if truthy else "f"
    return OPTION_IDS[chosen]


@pytest.mark.parametrize("version,generator,fixture,strategy",
                         INTERACTION_MATRIX, ids=INTERACTION_IDS)
def test_interactions_record_every_question(version, generator, fixture,
                                            strategy, package_factory,
                                            open_sco):
    """One interaction per question, matching the Python model for the
    strategy actually played - and never the answer key."""
    package = package_factory(version, generator, fixture)
    questions = package.assessment.questions
    correct = (strategy == "all-correct")
    expected_score = (package.perfect_percentage if correct
                      else package.naive_percentage)

    with open_sco(package) as session:
        _answer(session, package, correct=correct)
        _wait_for_result(
            session,
            "pass" if expected_score >= package.passing_score else "fail")
        recording = session.recording()
    assert not session.errors, session.errors

    interactions = recording.interactions()
    assert recording.interaction_indices() == list(range(len(questions))), (
        "expected {0} interactions indexed from 0, got {1}".format(
            len(questions), sorted(interactions)))

    for index, question in enumerate(questions):
        written = interactions[index]
        chosen = question.correct_answer if correct else 0

        assert written["id"] == question.id, written
        assert written["type"] == _expected_interaction_type(question), written
        assert written["weighting"] == str(question.points), written
        assert written[RESPONSE_ELEMENT[version]] == \
            _expected_response(question, chosen, version), written

        expected_result = ("correct" if chosen == question.correct_answer
                           else WRONG[version])
        assert written["result"] == expected_result, (
            "{0}: the model says {1}, the LMS recorded {2}".format(
                question.id, expected_result, written["result"]))

        assert re.fullmatch(LATENCY_PATTERN[version], written["latency"]), \
            written["latency"]
        when = written[WHEN_ELEMENT[version]]
        assert re.fullmatch(WHEN_PATTERN[version], when), when

        if version == "2004":
            description = written["description"]
            assert len(description) <= 250, len(description)
            assert description == " ".join(question.text.split())[:250]
            kind = (question.source_ref or {}).get("kind")
            if kind:
                assert written["objectives.0.id"] == kind, written
        else:
            # 1.2 defines neither element, and every 1.2 interaction element
            # is write-only - nothing here reads one back.
            assert "description" not in written, written
            assert "timestamp" not in written, written

        # The answer key must never travel to the LMS through the learner's
        # browser, and correct_responses is exactly that key.
        assert not any(field.startswith("correct_responses")
                       for field in written), written

    # suspend_data: parseable, inside the cap, and carrying what was answered -
    # nothing about which answer was right.
    suspend = recording.last_set("cmi.suspend_data")
    assert suspend is not None, recording.pretty()
    assert len(suspend) <= 4096, len(suspend)
    parsed = json.loads(suspend)
    assert set(parsed) == {"attempt", "responses"}, parsed
    assert parsed["attempt"] == 1, parsed
    assert parsed["responses"] == {
        question.id: _expected_response(
            question, question.correct_answer if correct else 0, version)
        for question in questions
    }, parsed


@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_no_interaction_is_written_after_the_session_ends(version,
                                                          package_factory,
                                                          open_sco):
    """Interactions are evidence only if the LMS accepted them.  A write after
    LMSFinish/Terminate is rejected, and the question then silently disappears
    from the training record."""
    package = package_factory(version)
    finish = "Terminate" if version == "2004" else "LMSFinish"

    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    finish_at = recording.index_of(finish)
    assert finish_at is not None, recording.pretty()

    last_interaction = None
    for position, call in enumerate(recording.calls):
        if call["fn"] in ("SetValue", "LMSSetValue") and \
                call["args"][0].startswith("cmi.interactions."):
            last_interaction = position
    assert last_interaction is not None, recording.pretty()
    assert last_interaction < finish_at, (
        "an interaction was written after the session ended\n"
        + recording.pretty())

    commit = "Commit" if version == "2004" else "LMSCommit"
    assert any(call["fn"] == commit
               for call in recording.calls[last_interaction:]), (
        "the interactions were never committed\n" + recording.pretty())

    for call in recording.interaction_writes():
        assert call["ret"] == "true", call
        assert call["argTypes"] == ["string", "string"], call


@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_an_unanswered_question_is_recorded_as_neutral(version,
                                                       package_factory,
                                                       open_sco):
    """A skipped question is still an interaction: the auditor needs to see
    that it was put to the learner and left blank."""
    package = package_factory(version)
    skipped = package.assessment.questions[1]
    total = sum(question.points for question in package.assessment.questions)
    # Math.round, as the page computes it.
    percentage = int(math.floor(((total - skipped.points) / total) * 100 + 0.5))

    with open_sco(package) as session:
        _answer(session, package, correct=True, skip={skipped.id})
        _wait_for_result(
            session, "pass" if percentage >= package.passing_score else "fail")
        recording = session.recording()

    interactions = recording.interactions()
    assert len(interactions) == len(package.assessment.questions)

    written = interactions[1]
    assert written["id"] == skipped.id
    assert written["result"] == "neutral", written
    # No response element at all: "" is not a legal choice/true-false value.
    assert RESPONSE_ELEMENT[version] not in written, written
    assert written["weighting"] == str(skipped.points)

    for index in range(len(package.assessment.questions)):
        if index == 1:
            continue
        assert interactions[index]["result"] == "correct", interactions[index]

    parsed = json.loads(recording.last_set("cmi.suspend_data"))
    assert skipped.id not in parsed["responses"], parsed
    assert not session.errors, session.errors


@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_no_interactions_are_written_without_an_lms(version, package_factory,
                                                    open_sco):
    """No API, no interaction record, no exception - the page still scores."""
    package = package_factory(version)
    with open_sco(package, version=None) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        assert "You Passed" in session.sco.locator("#results").inner_text()
        assert not session.errors, session.errors


@pytest.mark.parametrize("version", ["1.2", "2004"])
def test_suspend_data_counts_the_attempt(version, package_factory, open_sco):
    """Relaunching after a recorded attempt increments the counter instead of
    pretending every submission is the first."""
    package = package_factory(version)
    with open_sco(package) as session:
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        assert json.loads(session.recording().last_set(
            "cmi.suspend_data"))["attempt"] == 1

        session.page.evaluate(
            "() => { window.__scormState.initialized = false;"
            " window.__scormState.terminated = false;"
            " window.__scormCalls = []; }")
        session.sco.locator("body").evaluate("() => window.location.reload()")
        session.sco.locator("#submit-button").wait_for(timeout=20000)
        _answer(session, package, correct=True)
        _wait_for_result(session, "pass")
        recording = session.recording()

    assert json.loads(recording.last_set("cmi.suspend_data"))["attempt"] == 2, \
        recording.pretty()
