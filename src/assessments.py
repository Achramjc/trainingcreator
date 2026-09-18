"""
Assessment Generator - Create verification questions and quizzes from SOP content.

ASSESSMENT INTEGRITY DESIGN (M0)
--------------------------------
Everything in this module exists to make one statement true: *a learner who has
not read the SOP fails the quiz*.  Six properties deliver that.

1. Deterministic shuffling with a balanced key.
   Every seed descends from ``document_key(title, version)`` so the same SOP
   revision always yields byte-identical output (reproducible builds are part of
   the validation story).  Distractor arrangement is seeded from
   sha256(doc_key | question_id), as required.

   The correct answer's *position* is not an independent per-question draw:
   independent draws are uniform only in expectation, and a three-question quiz
   can easily land every answer on option A.  Instead questions are grouped by
   option count, dealt cyclic positions inside each group, and the per-group
   offsets are chosen jointly - every combination is scored against the
   strategies a learner can actually execute ("always option 1", ..., "always the
   last option", plus whatever the fixed true/false answers contribute) and one
   of the combinations holding all of them at or below NAIVE_SCORE_CEILING is
   picked by document digest.  Filtering and then picking at random, rather than
   always taking the best, keeps the aggregate distribution uniform.
   See ``_assign_answer_positions``.

2. The answer key never reaches the learner as plaintext.
   The Python model holds the correct index after shuffling.  The learner payload
   (``to_learner_dict``) carries only a per-question salt and
   sha256(salt | normalised correct option text).  See ``src/answer_key.py`` for
   the honest limits of that - a static package can be brute-forced over 2-4
   options by anyone with dev tools, and server-side scoring is the M2 fix.

3. Distractors come from the source document.
   Wrong answers are other steps, other definitions, scope/responsibility
   sentences, or *altered* safety warnings ("Never" -> "Always", "must" ->
   "may optionally").  Same register, same length band, drawn from the same SOP.
   Any candidate that normalises to the correct answer, contains it, or is
   >= 88% similar to it is rejected.  There is no absurd filler: if a document
   cannot support four options we ship three, then two, and never invent
   nonsense.

4. Every option of a question opens in the same *register*.
   A human read of generated questions found the one giveaway none of the above
   catches: "What is the primary purpose of this procedure?" offered "This
   Standard Operating Procedure establishes ..." against three step bodies
   ("Confirm the label reconciliation record ..."), so the answer could be picked
   out by form alone.  Purpose and scope questions now draw only on prose about
   the procedure - scope, responsibilities, definitions and altered forms of
   those sentences, never step instructions - and are dropped, with a note, when
   the document cannot supply MIN_REGISTER_DISTRACTORS of them.  Step and
   definition questions prefer their own register and fall back only when a thin
   document leaves no choice.  See ``register_class``.

5. No two questions share their material.
   The same read found a quiz whose true/false item put altered warning W' under
   test while a later item asked "which statement CONTRADICTS a safety warning?"
   with W' as the correct answer - answer one, answer both - and a step question
   whose correct answer (the Step 6 body) was a distractor in the Step 1
   question, so knowing one struck an option off the other.  The blueprint keeps
   a ledger of the texts the selected answers turn on (a statement and its flip
   count as one piece of material, see ``related_texts``), skips candidates whose
   answer is already spoken for, and rebuilds distractor sets that would repeat
   another question's answer.  Repair may cost a question one option, never two;
   what cannot be repaired is counted in ``Assessment.leakage_count``, which is 0
   on every bundled and gallery document.

6. Coverage is a deterministic blueprint, not random.sample().
   Priority: medical-device required questions, then >=1 safety question when
   warnings exist, then >=1 sequence question when there are >=3 steps, then
   step content spread across the procedure (midpoint bisection order, not the
   first N steps), then purpose/scope, then definitions.

True/false questions are kept but roughly half of the selected ones are *false*
statements (an altered warning or a definition attached to the wrong term), so
"always True" fails too.  The broken ``ordering`` question type is gone: it
emitted a list of step numbers as ``correct_answer`` while the renderer compared
a single radio value, so it was unanswerable.  It is replaced by a ``sequence``
question ("which action comes immediately after Step 3?") whose options are step
*titles* with the numbers stripped, so the answer cannot be read off the labels.
"""

import re
from difflib import SequenceMatcher
from itertools import product
from random import Random
from typing import Dict, List, Optional, Tuple

from .answer_key import (
    answer_hash as _hash_answer,
    document_key,
    make_salt,
    normalize_option_text,
    question_digest,
    seed_from_digest,
)
from .parser import SOPContent

# Option shaping -------------------------------------------------------------
MAX_OPTION_CHARS = 180
MAX_OPTIONS = 4
MIN_OPTIONS = 2
#: Appended by clip_option; counted against MAX_OPTION_CHARS, never added on top.
ELLIPSIS = "..."
#: A distractor at or above this similarity to the correct answer is discarded.
NEAR_IDENTICAL_RATIO = 0.88
#: Ceiling on the fraction of the available points any single fixed answering
#: strategy ("always option 1", "always the last option", ...) may collect.
#: Well under any realistic passing score, so a naive learner fails with margin.
NAIVE_SCORE_CEILING = 0.65
#: A purpose or scope question needs at least this many *same-register*
#: distractors (see ``register_class``).  With fewer the question is dropped
#: rather than asked: one purpose statement against a single step body is a
#: giveaway, and two options make it a coin flip on top.
MIN_REGISTER_DISTRACTORS = 2
#: Shortest assessment that can actually satisfy NAIVE_SCORE_CEILING.  With
#: fewer questions than this the points are too lumpy to spread: some fixed
#: strategy always lands on a heavy question plus a light one.  generate()
#: raises any smaller request to this and records it in Assessment.notes.
MIN_ASSESSMENT_QUESTIONS = 5

# Points.  A four-option item can be guessed 25% of the time, a true/false item
# 50%, so a true/false item is worth HALF a multiple-choice item of the same
# importance - crediting them equally over-rewards guessing and weakens the
# discrimination the assessment exists to provide.  Safety and compliance items
# are worth double within their kind.  (They used to be flat 1/2, which let a
# single two-point true/false question carry a third of a short quiz - and since
# "False" is inevitably longer than "True", that handed a third of the marks to
# anyone clicking the longest option.)
POINTS_CHOICE = 2
POINTS_TRUE_FALSE = 1
POINTS_SAFETY_CHOICE = 4
POINTS_SAFETY_TRUE_FALSE = 2


def scale_points_for_options(points: int, option_count: int) -> int:
    """Weight an item by the guess baseline it actually offers.

    A multiple-choice question that could only be given two options because the
    document had nothing else to offer *is* a true/false question, however it was
    authored, and must be weighted like one.  Leaving it at full weight put the
    heaviest question in a short quiz on a coin flip - and since such a question
    is usually one long real warning against one short altered one, it handed
    those points to anyone clicking the longest option.
    """
    if option_count <= 2:
        return max(1, points // 2)
    return points

#: Largest share of the points that may sit on questions whose correct option is
#: the longest (or the shortest) of the options offered.  Enforced by swapping a
#: selected question for another from the SAME category, so coverage is
#: unchanged.  True/false items count half their points towards each, because
#: "False" is always the longer option and the truth values are balanced.
LENGTH_TELL_CEILING = 0.35
#: Bound on the repair loop above; it is a best-effort improvement, not a
#: guarantee - a document may simply not contain a better-shaped alternative.
MAX_LENGTH_REPAIR_SWAPS = 12

#: Statement flips used to build *false* variants of real document sentences.
#: Order matters - longer / more specific patterns first, and the negative forms
#: ("must not") must be tried before the positive ones ("must").
_STATEMENT_FLIPS: Tuple[Tuple[str, str], ...] = (
    ("must not", "must"),
    ("shall not", "shall"),
    ("may not", "may"),
    ("do not", "you may"),
    ("does not", "does"),
    ("is not", "is"),
    ("never", "always"),
    ("always", "never"),
    ("must", "may optionally"),
    ("shall", "may optionally"),
    ("is required", "is optional"),
    ("are required", "are optional"),
    ("required", "optional"),
    ("prohibited", "permitted"),
    ("applies to all", "applies only to some"),
    ("all personnel", "only supervisors"),
    ("every", "any single"),
    ("prior to", "after"),
    ("before", "after"),
    ("at least", "at most"),
    ("all", "some"),
    ("may", "must never"),
)


# ---------------------------------------------------------------------------
# Register ("opening form") classification
#
# A human read of the generated questions found the giveaway this fixes: "What
# is the primary purpose of this procedure?" offered one option beginning "This
# Standard Operating Procedure establishes..." against three step bodies
# ("Confirm the label reconciliation record...").  Nobody has to know the SOP to
# see which of those is a purpose statement - the *register* answers the
# question.  Every option of a question must therefore open in the same form as
# the correct one, so the learner has to compare content.
#
# The classifier is a first-token heuristic, not a parser.  It only has to be
# consistent and discriminating enough to keep prose statements and imperative
# instructions apart; ``tests/test_assessments.py`` asserts the property it
# exists for.
# ---------------------------------------------------------------------------
#: "This SOP applies to...", "All personnel must...", "The compiled set of
#: records..." - prose *about* the procedure.
REGISTER_STATEMENT = "statement"
#: "Confirm the traveler...", "Do not bypass..." - an instruction to the reader.
REGISTER_IMPERATIVE = "imperative"
#: "Quality Engineers: verify test records..." - a RESPONSIBILITIES line, which
#: names an actor and reads like neither of the above.
REGISTER_ROLE = "role"

#: A short capitalised label followed by a colon: the shape of a
#: RESPONSIBILITIES entry.
_ROLE_LABEL_RE = re.compile(
    r"^[A-Z][\w/&.\-]*(?:\s+[A-Za-z][\w/&.\-]*){0,4}:\s+\S")

#: First words of a sentence that states something rather than instructing.
#: Determiners, pronouns and role nouns only - never a word that doubles as a
#: bare verb ("Record the reading ..." is an instruction, and a noun subject like
#: "Records are retained ..." is caught by _PREDICATE_MARKERS instead).
_STATEMENT_OPENERS = frozenset("""
this these those that the a an it its there they he she we our
all any each every both either neither none no nothing anyone everyone someone
personnel operators operator employees staff technicians technician
supervisors supervisor engineers engineer inspectors inspector management
""".split())

#: Bare verbs that open an instruction.  Drawn from the imperative openings the
#: bundled and gallery SOPs actually use; an unknown opener falls through to the
#: shape rules below rather than being guessed at.
_IMPERATIVE_VERBS = frozenset("""
confirm verify record review complete pull retrieve check inspect ensure make
document attach apply remove install seat loosen tighten torque bleed clean
wear don doff label print sign initial file scan measure weigh calibrate
adjust open close start stop shut isolate lock tag notify report escalate
contact obtain collect place position transfer move store dispose discard
rinse wipe flush purge sample test run execute perform follow repeat compare
calculate reconcile count enter log update submit approve reject segregate
quarantine hold release stage load unload mount align set select switch press
push pull turn rotate connect disconnect reconnect disable enable reset
restore energize wait allow observe monitor read write add mix dilute
sanitize sterilize decontaminate assemble disassemble reassemble replace
accession identify locate flag capture photograph annotate initiate activate
deactivate disengage engage engrave sort restart power secure authorize
never always do dont cross-reference spot-check double-check back-flush
""".split())

#: A second token that turns the first into a subject rather than a verb:
#: "Records ARE retained", "Personnel MUST wear", "Verification IS the act of".
_PREDICATE_MARKERS = frozenset("""
is are was were will shall must should may can cannot has have had does
includes include means covers applies consists contains exists remains
provides requires ensures defines governs describes establishes
""".split())

#: A second token that marks the first as a bare transitive verb taking a
#: direct object: "Accession THE specimen", "Cross-reference EACH component".
_OBJECT_DETERMINERS = frozenset("""
the a an this these those each all every any both either your its it them
""".split())

#: Leading clause openers.  "For each unit, record the reading" instructs; the
#: instruction is what follows the comma.
_CLAUSE_OPENERS = frozenset("""
if when once after before while during upon unless until whenever where
using with for as in on at given following prior
""".split())


def _words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", text)


def register_class(text) -> str:
    """Classify the *opening form* of a sentence.

    Returns one of :data:`REGISTER_STATEMENT`, :data:`REGISTER_IMPERATIVE`,
    :data:`REGISTER_ROLE`.  Options offered for one question must all share a
    class, or the correct answer can be picked out by form alone.

    Deliberately conservative: an opener the heuristic does not recognise is
    treated as a statement (the commoner prose shape) unless the sentence looks
    like "verb + object".
    """
    t = _clean(text)
    if not t:
        return REGISTER_STATEMENT
    if _ROLE_LABEL_RE.match(t):
        return REGISTER_ROLE

    head = t
    tokens = _words(head)
    if tokens and tokens[0].lower() in _CLAUSE_OPENERS and "," in head:
        head = head.split(",", 1)[1]
        tokens = _words(head)
    # Leading adverbs describe how, not what: "Visually inspect ...",
    # "Immediately notify ...".
    while len(tokens) > 1 and tokens[0].lower().endswith("ly"):
        tokens = tokens[1:]
    if not tokens:
        return REGISTER_STATEMENT

    first = tokens[0].lower()
    second = tokens[1].lower() if len(tokens) > 1 else ""
    if first in _STATEMENT_OPENERS:
        return REGISTER_STATEMENT
    if first in _IMPERATIVE_VERBS:
        return REGISTER_IMPERATIVE
    if second in _PREDICATE_MARKERS:
        return REGISTER_STATEMENT
    if second in _OBJECT_DETERMINERS:
        return REGISTER_IMPERATIVE
    return REGISTER_STATEMENT


def same_register(correct, candidates) -> List[str]:
    """Only the candidates whose opening form matches the correct answer's."""
    target = register_class(correct)
    return [c for c in candidates if register_class(c) == target]


def register_preferred(correct, candidates,
                       minimum: int = MAX_OPTIONS - 1) -> List[str]:
    """Same-register candidates, topped up with the rest only if too few.

    Used where the material is naturally homogeneous (other steps for a step
    question, other definitions for a definition question) and a thin document
    must still produce a question: preferring the matching register removes the
    tell whenever the document can afford it, without dropping coverage when it
    cannot.  Purpose and scope questions use the strict :func:`same_register`
    instead, because the tell there is total.
    """
    same = same_register(correct, candidates)
    if len(same) >= minimum:
        return same
    target = register_class(correct)
    return same + [c for c in candidates if register_class(c) != target]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _clean(text) -> str:
    """Collapse whitespace; tolerate None and non-strings."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def clip_option(text, limit: int = MAX_OPTION_CHARS) -> str:
    """Clip an option to the shared length band, on a word boundary.

    The result is always <= ``limit`` INCLUDING the ellipsis.  The first version
    of this appended "..." after truncating to ``limit``, so clipped options came
    out at 176-183 characters instead of a flat cap.  Options built from long
    step bodies then differed by a few characters, and because distractors were
    chosen closest-first by length the correct answer ended up the longest option
    on almost every step question - "always click the longest option" scored
    87.5% on the bundled sample SOP.
    """
    t = _clean(text)
    if len(t) <= limit:
        return t
    budget = limit - len(ELLIPSIS)
    # +1 so a space sitting exactly on the boundary still counts as one.
    cut = t[:budget + 1]
    space = cut.rfind(" ")
    cut = cut[:space] if space > budget * 0.6 else cut[:budget]
    return cut.rstrip(" ,;:.-") + ELLIPSIS


def _sentences(text, min_len: int = 20) -> List[str]:
    """Split prose into sentences long enough to stand alone as an option."""
    t = _clean(text)
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])\s+", t)
    return [p.strip() for p in parts if len(p.strip()) >= min_len]


def alter_statement(text) -> Optional[str]:
    """Return a *false* variant of a true statement, or None if we cannot make
    one honestly.

    Used for safety distractors and for false true/false items.  Only the first
    matching flip is applied, so the result stays close to the original in
    register and length - the learner has to know the content, not spot the odd
    sentence out.  Returning None (rather than inventing filler) is deliberate:
    a statement we cannot reliably negate is simply not used.
    """
    t = _clean(text)
    if not t:
        return None
    for old, new in _STATEMENT_FLIPS:
        match = re.search(r"\b" + re.escape(old) + r"\b", t, re.IGNORECASE)
        if not match:
            continue
        found = match.group(0)
        replacement = new
        if found[:1].isupper():
            replacement = replacement[:1].upper() + replacement[1:]
        return t[: match.start()] + replacement + t[match.end():]
    return None


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def related_texts(text) -> set:
    """A statement and its honest negation, normalised as options.

    Two options that are each other's flip are not independent material: a
    learner who knows the real warning knows the altered one is false.  Counting
    them as the same text is what stops one assessment asking "true or false:
    <altered warning W'>" and, two questions later, "which statement CONTRADICTS
    a safety warning?" with W' as the correct option - and what stops one
    question's correct answer being the twin of another question's distractor.
    """
    out = set()
    for variant in (text, alter_statement(text)):
        if not variant:
            continue
        norm = normalize_option_text(clip_option(variant))
        if norm:
            out.add(norm)
    return out


def pick_distractors(correct: str, candidates, rng: Random,
                     limit: int = MAX_OPTIONS - 1) -> List[str]:
    """Choose up to ``limit`` distractors for ``correct`` from ``candidates``.

    Rejects anything empty, duplicated, identical or near-identical to the
    correct answer, or that contains / is contained by it.

    Survivors are then chosen so the correct answer sits in the MIDDLE of the
    length band: closest-first alternately from the candidates longer than it and
    the candidates shorter than it.  Taking simply the closest by absolute
    difference is not enough - when the correct answer is near either end of the
    pool's length distribution every distractor lands on the same side of it, and
    "always click the longest option" becomes a winning strategy.  Straddling
    puts the correct answer's length rank at roughly 1/k, the same as chance.
    """
    correct_clean = clip_option(correct)
    correct_norm = normalize_option_text(correct_clean)
    if not correct_norm:
        return []

    seen = {correct_norm}
    pool: List[str] = []
    for raw in candidates:
        cand = clip_option(raw)
        norm = normalize_option_text(cand)
        if not norm or norm in seen:
            continue
        if norm in correct_norm or correct_norm in norm:
            continue
        if _similarity(norm, correct_norm) >= NEAR_IDENTICAL_RATIO:
            continue
        seen.add(norm)
        pool.append(cand)

    if not pool:
        return []

    target_len = len(correct_clean)
    # Shuffle first, then stable-sort by distance: equal-distance candidates are
    # tie-broken pseudo-randomly while the ordering stays reproducible.
    rng.shuffle(pool)
    longer = sorted((c for c in pool if len(c) > target_len),
                    key=lambda c: len(c) - target_len)
    shorter = sorted((c for c in pool if len(c) <= target_len),
                     key=lambda c: target_len - len(c))

    chosen: List[str] = []
    take_longer = True
    while len(chosen) < limit and (longer or shorter):
        source = longer if (take_longer and longer) or not shorter else shorter
        chosen.append(source.pop(0))
        take_longer = not take_longer
    return chosen


# ---------------------------------------------------------------------------
# Procedure-step helpers
#
# The parser contract grew ``title`` / ``body`` / ``source_lines`` and ``content``
# became the FULL step text.  These helpers prefer the new keys and degrade
# cleanly to the old "content is just the heading" shape, so the generator works
# against either parser.
# ---------------------------------------------------------------------------
def step_number(proc: Dict) -> str:
    return _clean(proc.get("step_number", ""))


def step_title(proc: Dict) -> str:
    """Heading text of a step."""
    title = _clean(proc.get("title"))
    if title:
        return title
    content = str(proc.get("content", "") or "")
    return _clean(content.split("\n", 1)[0])


def step_body(proc: Dict) -> str:
    """Instruction text underneath the heading; "" when the SOP has none."""
    body = proc.get("body")
    if body is not None:
        return _clean(body)
    content = str(proc.get("content", "") or "")
    if "\n" in content:
        return _clean(content.split("\n", 1)[1])
    return ""


def step_sort_key(proc: Dict, doc_index: int):
    """Natural sort key for a step number.

    Step numbers are not integers.  Numbered-convention SOPs legitimately use
    "4.1", "4.2", ... "4.10", and "4.10" must sort after "4.9", not next to
    "4.1".  Anything we cannot read as a dotted number keeps document order
    instead of raising - the old ``int(x.get('step_number', 0))`` blew up with
    ValueError on the first "4.3" it saw.
    """
    raw = step_number(proc)
    parts = [p for p in re.split(r"[.\-_]", raw) if p != ""]
    segments = []
    for part in parts:
        match = re.match(r"^(\d+)([A-Za-z]*)$", part.strip())
        if not match:
            return (1, (), doc_index)
        segments.append((int(match.group(1)), match.group(2).lower()))
    if not segments:
        return (1, (), doc_index)
    return (0, tuple(segments), doc_index)


def ordered_steps(procedures) -> List[Dict]:
    """Procedure steps in execution order (natural step-number order)."""
    items = list(procedures or [])
    return [p for _, p in sorted(
        ((step_sort_key(p, i), p) for i, p in enumerate(items)),
        key=lambda pair: pair[0],
    )]


def _step_option_text(proc: Dict) -> str:
    """The text that represents a step as an answer option.

    Never includes the step number: "Which action comes after Step 3?" with
    options labelled "Step 4: ..." would be answerable from the labels alone.
    """
    return clip_option(step_title(proc) or step_body(proc))


def _step_answer_text(proc: Dict) -> str:
    """Text describing what a step requires - the body when we have one."""
    return clip_option(step_body(proc) or step_title(proc))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
class Question:
    """Represents a single assessment question.

    ``correct_answer`` is the index of the correct option *after* shuffling.  It
    is part of the SME/author-facing export (``to_dict``) and is deliberately
    absent from the learner-facing export (``to_learner_dict``).
    """

    def __init__(self, question_id: str, question_type: str, question_text: str,
                 options: List[str] = None, correct_answer: any = None,
                 explanation: str = "", points: int = 1,
                 salt: str = "", source_ref: Optional[Dict] = None,
                 topic: str = ""):
        self.id = question_id
        self.type = question_type  # multiple_choice, true_false, sequence
        self.text = question_text
        self.options = options or []
        self.correct_answer = correct_answer
        self.explanation = explanation
        self.points = points
        #: Public, per-question salt shipped with the package.
        self.salt = salt
        #: Provenance back to the source document (traceability, SME review).
        self.source_ref = source_ref or {}
        #: Non-revealing label shown in post-submission feedback.
        self.topic = topic

    @property
    def correct_option_text(self) -> str:
        """Text of the correct option, or "" if the key is not a valid index."""
        if isinstance(self.correct_answer, int) and not isinstance(self.correct_answer, bool):
            if 0 <= self.correct_answer < len(self.options):
                return self.options[self.correct_answer]
        return ""

    @property
    def answer_hash(self) -> str:
        """sha256(salt | normalised correct option). "" when unavailable."""
        text = self.correct_option_text
        if not self.salt or not text:
            return ""
        return _hash_answer(self.salt, text)

    def to_dict(self) -> Dict:
        """Author/SME-facing serialisation.

        MAY include answers - ``app.py`` and ``src/cli.py`` dump this as review
        JSON for subject-matter experts.  It must never be handed to a learner;
        use :meth:`to_learner_dict` for that.
        """
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "options": self.options,
            "correct_answer": self.correct_answer,
            "explanation": self.explanation,
            "points": self.points,
            "salt": self.salt,
            "answer_hash": self.answer_hash,
            "source_ref": self.source_ref,
            "topic": self.topic,
        }

    def to_learner_dict(self) -> Dict:
        """Learner-facing payload: no correct_answer, no explanation.

        Verification is by salted hash of the normalised option text.  See
        ``src/answer_key.py`` for what this does and does not protect against.
        """
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "options": list(self.options),
            "points": self.points,
            "salt": self.salt,
            "answer_hash": self.answer_hash,
            "topic": self.topic,
        }

    #: Alias - some callers speak of the "client payload".
    to_client_payload = to_learner_dict


class Assessment:
    """Collection of assessment questions"""

    def __init__(self):
        self.title: str = ""
        self.description: str = ""
        self.questions: List[Question] = []
        self.passing_score: int = 70  # Percentage
        self.time_limit: Optional[int] = None  # Minutes
        self.randomize_questions: bool = False
        #: Option order is randomised deterministically at *generation* time and
        #: baked into ``Question.options``; the renderer emits them as stored.
        #: Kept for backward compatibility and to record that it happened.
        self.randomize_options: bool = True
        #: How many questions the caller asked for.  ``len(questions)`` is what
        #: the source document could actually support, after the request has
        #: been raised to MIN_ASSESSMENT_QUESTIONS if it was below it.
        self.requested_questions: int = 0
        #: Human-readable warnings about how the request was satisfied, for the
        #: SME review JSON and the transparency report.
        self.notes: List[str] = []
        #: How many distractors still repeat another question's correct answer.
        #: Cross-question elimination ("I know Q2's answer, so that option in Q3
        #: is out") is a way to score without reading the SOP, so the generator
        #: rebuilds distractor sets to drive this to zero.  It can only be
        #: non-zero on a document too small to offer an alternative, and then it
        #: is reported rather than hidden.
        self.leakage_count: int = 0

    @property
    def total_points(self) -> int:
        return sum(q.points for q in self.questions)

    def to_dict(self) -> Dict:
        """Author/SME-facing serialisation (may include answers)."""
        return {
            "title": self.title,
            "description": self.description,
            "questions": [q.to_dict() for q in self.questions],
            "passing_score": self.passing_score,
            "time_limit": self.time_limit,
            "randomize_questions": self.randomize_questions,
            "randomize_options": self.randomize_options,
            "requested_questions": self.requested_questions,
            "notes": list(self.notes),
            "leakage_count": self.leakage_count,
        }

    def to_learner_dict(self) -> Dict:
        """Learner-facing payload.  The SCORM exporter uses only this."""
        return {
            "title": self.title,
            "description": self.description,
            "questions": [q.to_learner_dict() for q in self.questions],
            "passing_score": self.passing_score,
            "time_limit": self.time_limit,
        }

    to_client_payload = to_learner_dict


# ---------------------------------------------------------------------------
# Question candidates
#
# Generation happens in two phases.  Builders produce *candidates* in canonical
# form (correct answer first, true/false carrying both a true and a false
# variant).  Only after the coverage blueprint has chosen which candidates to
# use are truth values balanced and option positions assigned, because both of
# those are properties of the assessment as a whole rather than of one question.
# ---------------------------------------------------------------------------
class _Candidate:
    __slots__ = ("category", "order", "kind", "qid", "points", "topic",
                 "source_ref", "prompt", "correct", "distractors", "explanation",
                 "true_text", "false_text", "explanation_true", "explanation_false",
                 "sources", "true_material", "false_material",
                 "min_distractors")

    def __init__(self, category, order, kind, qid, points=1, topic="", source_ref=None):
        self.category = category
        self.order = order
        self.kind = kind            # "mc" | "sequence" | "tf"
        self.qid = qid
        self.points = points
        self.topic = topic
        self.source_ref = source_ref or {}
        self.prompt = ""
        self.correct = ""
        self.distractors: List[str] = []
        self.explanation = ""
        self.true_text = ""
        self.false_text: Optional[str] = None
        self.explanation_true = ""
        self.explanation_false = ""
        #: Every piece of material the distractors were drawn from, so a
        #: distractor that turns out to answer another question can be replaced
        #: (see ``AssessmentGenerator._repair_cross_question_leakage``).
        self.sources: List[str] = []
        #: Fewest distractors this question may be asked with.  Most questions
        #: can honestly shrink to two options (the points scale with the guess
        #: baseline), but a purpose or scope question may not: see
        #: MIN_REGISTER_DISTRACTORS.
        self.min_distractors = MIN_OPTIONS - 1
        #: For a true/false candidate, the substantive statement under test in
        #: each truth value, *without* the "True or False: ..." wrapper - that
        #: is the text another question must not reuse.
        self.true_material = ""
        self.false_material = ""

    @classmethod
    def choice(cls, category, order, qid, prompt, correct, distractors,
               explanation, kind="mc", points=1, topic="", source_ref=None,
               sources=None, min_distractors=MIN_OPTIONS - 1):
        cand = cls(category, order, kind, qid, points, topic, source_ref)
        cand.prompt = prompt
        cand.correct = correct
        cand.distractors = list(distractors)
        cand.explanation = explanation
        cand.sources = list(sources or distractors)
        # Rebuilding distractors to keep the questions independent may cost a
        # question one option - never more.  Two options is a coin flip, and a
        # quiz of coin flips hands the marks to "always click the longest
        # option"; a question that cannot keep its width is dropped in favour of
        # another (see _repairable), and a document that cannot fill the request
        # that way reports the shortfall in requested_questions.
        cand.min_distractors = max(min_distractors, len(cand.distractors) - 1)
        return cand

    @classmethod
    def true_false(cls, category, order, qid, true_text, false_text,
                   explanation_true, explanation_false, points=1, topic="",
                   source_ref=None, true_material="", false_material=""):
        cand = cls(category, order, "tf", qid, points, topic, source_ref)
        cand.true_text = true_text
        cand.false_text = false_text
        cand.explanation_true = explanation_true
        cand.explanation_false = explanation_false
        cand.true_material = true_material
        cand.false_material = false_material
        return cand

    @property
    def truth_is_fixed(self) -> bool:
        """True when no honest false variant exists, so the answer must be True."""
        return self.kind == "tf" and not self.false_text

    def answer_material(self) -> set:
        """Normalised texts the answer to this question turns on.

        For a multiple-choice question that is the correct option.  For a
        true/false question it is BOTH statements it could put under test: the
        truth value is assigned after selection, and either statement appearing
        in another question would let one item answer the other.  This is the
        set the blueprint refuses to reuse - the defect being fixed is a quiz
        that asked "true or false: <altered warning W'>" and, two questions
        later, "which statement CONTRADICTS a safety warning?" with W' as the
        correct option.

        Each statement is expanded to its flip as well (see
        :func:`related_texts`): "Do not release a lot ..." and "You may release a
        lot ..." are the same material asked two ways.
        """
        if self.kind == "tf":
            texts = (self.true_material or self.true_text,
                     self.false_material or self.false_text)
        else:
            texts = (self.correct,)
        material: set = set()
        for text in texts:
            material |= related_texts(text)
        return material

    def materialize(self, doc_key: str, truth: Optional[bool] = None) -> Question:
        salt = make_salt(doc_key, self.qid)
        if self.kind == "tf":
            is_true = True if truth is None else bool(truth)
            if not self.false_text:
                is_true = True
            text = self.true_text if is_true else self.false_text
            source_ref = dict(self.source_ref)
            source_ref["statement_is_true"] = is_true
            return Question(
                question_id=self.qid,
                question_type="true_false",
                question_text=text,
                options=["True", "False"],
                correct_answer=0 if is_true else 1,
                explanation=self.explanation_true if is_true else self.explanation_false,
                points=self.points,
                salt=salt,
                source_ref=source_ref,
                topic=self.topic,
            )

        q_type = "sequence" if self.kind == "sequence" else "multiple_choice"
        options = [self.correct] + list(self.distractors)
        # Canonical form: correct option first.  _assign_answer_positions moves it.
        return Question(
            question_id=self.qid,
            question_type=q_type,
            question_text=self.prompt,
            options=options,
            correct_answer=0,
            explanation=self.explanation,
            points=scale_points_for_options(self.points, len(options)),
            salt=salt,
            source_ref=dict(self.source_ref),
            topic=self.topic,
        )


def _candidate_points(candidate: "_Candidate") -> int:
    """Points this candidate will carry once materialised."""
    if candidate.kind == "tf":
        return candidate.points
    return scale_points_for_options(candidate.points,
                                    1 + len(candidate.distractors))


def _length_extremes(candidate: "_Candidate") -> Tuple[bool, bool]:
    """(correct option is the longest, correct option is the shortest)."""
    lengths = [len(candidate.correct)] + [len(d) for d in candidate.distractors]
    if len(lengths) < 2:
        return (False, False)
    return (len(candidate.correct) == max(lengths),
            len(candidate.correct) == min(lengths))


def _length_tell_weight(chosen: List["_Candidate"], extreme: int) -> float:
    """Points a learner clicking only the longest (0) or shortest (1) option wins.

    True/false items count half their points towards each: "False" is inevitably
    the longer of the two, and the truth values are balanced across the selected
    items, so roughly half of them fall on each side.
    """
    weight = 0.0
    for candidate in chosen:
        if candidate.kind == "tf":
            weight += _candidate_points(candidate) / 2.0
        elif _length_extremes(candidate)[extreme]:
            weight += _candidate_points(candidate)
    return weight


def _assign_truth_values(candidates: List[_Candidate]) -> List[Optional[bool]]:
    """Balance true/false statements across the *selected* true/false questions.

    "Always True" has to fail as surely as "always option A", so at least half of
    the true/false items a learner sees are false statements.  Items whose truth
    is fixed (no honest negation available, or a compliance question whose answer
    must be True) are counted first and the flexible ones compensate.
    """
    truths: List[Optional[bool]] = [None] * len(candidates)
    true_count = false_count = 0

    for idx, cand in enumerate(candidates):
        if cand.kind == "tf" and cand.truth_is_fixed:
            truths[idx] = True
            true_count += 1

    for idx, cand in enumerate(candidates):
        if cand.kind != "tf" or cand.truth_is_fixed:
            continue
        if false_count <= true_count:
            truths[idx] = False
            false_count += 1
        else:
            truths[idx] = True
            true_count += 1
    return truths


def naive_strategies(max_options: int) -> List[Tuple[str, int, str]]:
    """The answering strategies a learner can execute without reading the SOP.

    "Always click option 3" is not well defined on a two-option question, and the
    first version of this model scored it as collecting nothing there.  A real
    learner clicks *something*, so each aim-at-index strategy is modelled twice -
    falling back to the first option, and to the last one - and the layout has to
    survive whichever the learner does.  Missing that is how "always option 3"
    reached 100% on a three-question quiz whose smallest question had two
    options.

    Strategies that do not depend on option *position* ("always the longest
    option", "always True") cannot be influenced by this layout search; they are
    handled by distractor length-banding and true/false truth balancing, and
    asserted directly in the test suite.
    """
    strategies: List[Tuple[str, int, str]] = []
    for aim in range(max_options):
        for fallback in ("first", "last"):
            strategies.append(("index", aim, fallback))
    strategies.append(("last", 0, "last"))
    return strategies


def strategy_pick(strategy: Tuple[str, int, str], option_count: int) -> int:
    """Which option index this strategy selects on a question of this size."""
    kind, aim, fallback = strategy
    if kind == "last":
        return option_count - 1
    if aim < option_count:
        return aim
    return 0 if fallback == "first" else option_count - 1


def _worst_strategy_share(group_offsets, groups, option_counts,
                          true_false_questions, total_points, max_options):
    """Largest share of the points any modelled naive strategy would collect."""
    worst = 0
    for strategy in naive_strategies(max_options):
        earned = 0
        for question in true_false_questions:
            if strategy_pick(strategy, len(question.options)) == question.correct_answer:
                earned += question.points
        for option_count, offset in zip(option_counts, group_offsets):
            pick = strategy_pick(strategy, option_count)
            for rank, question in enumerate(groups[option_count]):
                if (offset + rank) % option_count == pick:
                    earned += question.points
        if earned > worst:
            worst = earned
    return worst / float(total_points)


def _assign_answer_positions(questions: List[Question], doc_key: str) -> None:
    """Place the correct option and shuffle the distractors, deterministically.

    Distractor arrangement is seeded from sha256(doc_key | question_id), as the
    M0 contract requires, so the same SOP revision always produces the same
    package.

    The correct answer's *position* is not drawn independently per question.
    Independent draws are uniform only in expectation, and a three question quiz
    can easily land every answer on option A - which is the defect being fixed.
    Instead:

      * questions are grouped by option count and, within a group, the correct
        positions are dealt as a cycle (offset, offset+1, ... mod k).  A cycle is
        perfectly balanced, so inside a group no position can hold more than
        ceil(n/k) of the answers;
      * the per-group offsets are then chosen *together*.  Every combination is
        scored against the naive strategies a learner can actually execute (see
        ``naive_strategies``: "always option N" with both realistic fallbacks for
        questions that have fewer options than that, and "always the last
        option"), including whatever the fixed true/false answers already
        contribute, and the combinations that hold every strategy at or below
        NAIVE_SCORE_CEILING are kept.  One survivor is picked by document digest.

    Choosing uniformly among the survivors rather than taking the single best is
    deliberate.  Always minimising would push the answer towards the middle
    positions of small assessments and skew the aggregate distribution; filtering
    then picking at random keeps four-option answers near 25% per position while
    still ruling out the gameable layouts.  When a document is so thin that no
    combination clears the ceiling (one or two questions, nothing to balance
    against) the least-bad combinations are used instead - and the naive learner
    tests document what that floor actually is.

    True/false questions keep their natural "True, False" order; their integrity
    comes from balancing the statements' truth values (see _assign_truth_values).
    """
    shuffled = [q for q in questions
                if q.type != "true_false" and len(q.options) >= MIN_OPTIONS]
    if not shuffled:
        return

    groups: Dict[int, List[Question]] = {}
    for question in shuffled:
        groups.setdefault(len(question.options), []).append(question)
    for group in groups.values():
        group.sort(key=lambda q: question_digest(doc_key, q.id))
    option_counts = sorted(groups)

    # True/false answers are already fixed, so they are part of what a naive
    # learner collects and the multiple-choice layout has to work around them.
    true_false_questions = [q for q in questions
                            if q.type == "true_false"
                            and isinstance(q.correct_answer, int)]

    total_points = sum(q.points for q in questions) or 1
    max_options = max(option_counts)

    scored: List[Tuple[float, Tuple[int, ...]]] = []
    for combination in product(*(range(k) for k in option_counts)):
        worst = _worst_strategy_share(
            combination, groups, option_counts, true_false_questions,
            total_points, max_options)
        scored.append((worst, combination))

    survivors = [combo for worst, combo in scored if worst <= NAIVE_SCORE_CEILING]
    if not survivors:
        floor = min(worst for worst, _ in scored)
        survivors = [combo for worst, combo in scored if worst == floor]

    selector = int(question_digest(doc_key, "answer-positions")[:8], 16)
    chosen = survivors[selector % len(survivors)]

    for option_count, offset in zip(option_counts, chosen):
        for rank, question in enumerate(groups[option_count]):
            position = (offset + rank) % option_count
            rng = Random(seed_from_digest(question_digest(doc_key, question.id)))
            correct_text = question.options[0]
            distractors = list(question.options[1:])
            rng.shuffle(distractors)
            question.options = (
                distractors[:position] + [correct_text] + distractors[position:]
            )
            question.correct_answer = position


def _spread_indices(count: int) -> List[int]:
    """Indices in midpoint-bisection order, so any prefix is spread out.

    Used to pick step-content questions: taking the first three steps of a nine
    step SOP tests the opening and nothing else.
    """
    if count <= 0:
        return []
    result: List[int] = []
    queue = [(0, count - 1)]
    while queue:
        low, high = queue.pop(0)
        if low > high:
            continue
        mid = (low + high) // 2
        result.append(mid)
        queue.append((low, mid - 1))
        queue.append((mid + 1, high))
    return result


def _interleave(primary: List, secondary: List, rotate: int = 0) -> List:
    """Alternate two ordered lists, primary first.

    ``rotate`` shifts the second list before interleaving.  The two lists
    normally hold the SAME material in two forms - term 3's definition as a
    multiple-choice question and as a true/false statement - and the blueprint
    refuses to use one text twice (see ``_select_by_blueprint``), so pairing
    ``primary[i]`` with ``secondary[i]`` meant every true/false item was passed
    over immediately after its own multiple-choice twin had been taken, and
    assessments lost the form altogether.  Rotating by one keeps both forms in
    play, on different material.
    """
    secondary = list(secondary)
    if rotate and secondary:
        offset = rotate % len(secondary)
        secondary = secondary[offset:] + secondary[:offset]
    out = []
    for i in range(max(len(primary), len(secondary))):
        if i < len(primary):
            out.append(primary[i])
        if i < len(secondary):
            out.append(secondary[i])
    return out


class AssessmentGenerator:
    """Generate assessment questions from SOP content"""

    #: Order in which the blueprint tops up an assessment once the mandatory
    #: coverage is in place.  "step" appears repeatedly because step content is
    #: the bulk of any SOP.
    FILL_CYCLE = ("step", "purpose_scope", "safety", "step", "definition",
                  "sequence", "step")

    #: Order questions are presented in (document order, compliance last).
    PRESENTATION_RANK = {
        "purpose_scope": 0,
        "definition": 1,
        "step": 2,
        "sequence": 3,
        "safety": 4,
        "md_required": 5,
    }

    def __init__(self):
        self.question_templates = self._load_question_templates()
        #: Notes raised while building and selecting candidates (a question the
        #: document could not support honestly, material skipped to keep the
        #: questions independent).  Reset by every ``generate()`` call and copied
        #: onto the Assessment, so the SME sees *why* a question is missing.
        self._build_notes: List[str] = []

    def _note(self, text: str) -> None:
        if text not in self._build_notes:
            self._build_notes.append(text)

    # -- public API ---------------------------------------------------------
    def generate(self, sop_content: SOPContent, num_questions: int = 5,
                 passing_score: int = 70) -> Assessment:
        """
        Generate an assessment from SOP content.

        Deterministic: the same SOPContent and num_questions always produce an
        identical Assessment (``to_dict()`` compares equal).

        Args:
            sop_content: Parsed SOP content
            num_questions: Number of questions to generate.  Honoured exactly
                when the document supports it and it is at least
                MIN_ASSESSMENT_QUESTIONS; otherwise the whole pool is returned
                and ``assessment.requested_questions`` / ``assessment.notes``
                record the ask and what happened to it.
            passing_score: Minimum passing score percentage

        Returns:
            Assessment object with generated questions
        """
        requested = max(1, int(num_questions))
        # Below the floor the arithmetic simply does not work.  A three question
        # quiz in which one question is worth two of four points cannot be laid
        # out so that every fixed answering strategy stays under the passing
        # mark - with that few questions some strategy always collects the heavy
        # one plus a light one.  Raising the count is the honest fix; a three
        # question compliance quiz is not a valid competence check anyway.
        num_questions = max(requested, MIN_ASSESSMENT_QUESTIONS)
        doc_key = document_key(
            getattr(sop_content, "title", "") or "",
            getattr(sop_content, "version", "") or "",
        )

        assessment = Assessment()
        assessment.title = "{0} - Verification Assessment".format(
            getattr(sop_content, "title", "") or "Procedure"
        )
        assessment.description = (
            "Complete this assessment to verify your understanding of the procedure."
        )
        assessment.passing_score = passing_score
        assessment.requested_questions = requested
        if num_questions != requested:
            assessment.notes.append(
                "Requested {0} question(s); raised to the minimum of {1}. An "
                "assessment shorter than {1} questions cannot distribute its "
                "answers well enough to stop a learner passing by clicking the "
                "same option every time.".format(requested, MIN_ASSESSMENT_QUESTIONS)
            )

        self._build_notes = []
        pool = self._build_pool(sop_content, doc_key)
        chosen = self._select_by_blueprint(pool, num_questions, doc_key)
        # Length repair swaps whole questions; leakage repair rewrites the
        # distractors of the questions that survive.  Each can disturb the
        # other, so they alternate: two rounds is enough to settle in practice
        # and keeps generation bounded and deterministic.
        leakage = 0
        for _ in range(2):
            chosen = self._rebalance_length_extremes(chosen, pool, doc_key)
            leakage = self._repair_cross_question_leakage(chosen, doc_key)
        chosen.sort(key=lambda c: (self.PRESENTATION_RANK.get(c.category, 9),
                                   c.order, c.qid))

        truths = _assign_truth_values(chosen)
        questions = [cand.materialize(doc_key, truths[i])
                     for i, cand in enumerate(chosen)]
        _assign_answer_positions(questions, doc_key)

        assessment.questions = questions
        assessment.leakage_count = leakage
        if leakage:
            self._note(
                "{0} distractor(s) still repeat another question's correct "
                "answer: this document is too small to offer an alternative, so "
                "a learner who knows one answer can eliminate an option "
                "elsewhere. Exposed as assessment.leakage_count.".format(leakage))
        assessment.notes.extend(self._build_notes)
        return assessment

    # -- cross-question independence ---------------------------------------
    def _forbidden_for(self, candidate: "_Candidate",
                       group: List["_Candidate"]) -> set:
        """Texts ``candidate`` may not offer: the other questions' answers."""
        forbidden: set = set()
        for other in group:
            if other is not candidate:
                forbidden |= other.answer_material()
        return forbidden

    def _repairable(self, candidate: "_Candidate", forbidden: set,
                    doc_key: str) -> bool:
        """Can this question be asked without offering a forbidden text?

        Cheap when nothing leaks, which is the usual case.  Otherwise it runs the
        same ``pick_distractors`` call ``_repair_cross_question_leakage`` would,
        so selection and repair agree about what is possible.
        """
        if candidate.kind == "tf":
            return True
        if not any(related_texts(d) & forbidden for d in candidate.distractors):
            return True
        clean = [text for text in candidate.sources
                 if not (related_texts(text) & forbidden)]
        rng = Random(seed_from_digest(question_digest(doc_key, candidate.qid)))
        rebuilt = pick_distractors(candidate.correct, clean, rng,
                                   limit=len(candidate.distractors))
        return len(rebuilt) >= candidate.min_distractors

    def _keeps_questions_independent(self, chosen: List["_Candidate"],
                                     candidate: "_Candidate",
                                     doc_key: str) -> bool:
        """True when ``candidate`` can join ``chosen`` with nothing shared.

        Both directions matter.  The new question must not have to offer an
        answer the quiz already asks about, and the questions already selected
        must still have something to offer once this one's answer is spoken for -
        a two-option "which warning is stated?" whose only distractor is the flip
        of the new question's answer would be solved by solving the other.
        """
        group = chosen + [candidate]
        material = candidate.answer_material()
        for member in chosen:
            if member.answer_material() & material:
                return False
        for member in group:
            if not self._repairable(member, self._forbidden_for(member, group),
                                    doc_key):
                return False
        return True

    # -- blueprint ----------------------------------------------------------
    def _select_by_blueprint(self, pool: List[_Candidate], num_questions: int,
                             doc_key: str = "") -> List[_Candidate]:
        """Deterministic coverage blueprint - never random.sample().

        Priority: medical-device required questions, one safety question, one
        sequence question, then a repeating fill cycle.  Exhausted categories are
        skipped; if the whole pool is smaller than the request we return all of
        it and let ``requested_questions`` tell the caller.

        The blueprint also keeps the selected questions *independent*.  No text
        may be the answer to two of them, in either direction: the quiz that
        prompted this asked "true or false: <altered warning W'>" and, two
        questions later, "which statement CONTRADICTS a safety warning?" with W'
        as the correct option, so answering one answered the other.  A candidate
        whose answer turns on text already spoken for is skipped in favour of
        the next one in its category - which is also why a thin safety pool now
        yields one of the two question forms rather than both.
        """
        buckets: Dict[str, List[_Candidate]] = {}
        for cand in pool:
            buckets.setdefault(cand.category, []).append(cand)
        cursors = {key: 0 for key in buckets}
        chosen: List[_Candidate] = []
        #: Normalised texts the answers to the already-selected questions turn
        #: on.  Only ever grows, so a skipped candidate can never become usable
        #: later and its cursor is advanced past it.
        spoken_for: set = set()
        shared = 0

        def take(category: str) -> bool:
            nonlocal shared
            items = buckets.get(category)
            if not items:
                return False
            index = cursors[category]
            while index < len(items):
                candidate = items[index]
                index += 1
                material = candidate.answer_material()
                if material & spoken_for:
                    shared += 1
                    continue
                if not self._keeps_questions_independent(chosen, candidate,
                                                        doc_key):
                    shared += 1
                    continue
                cursors[category] = index
                spoken_for.update(material)
                chosen.append(candidate)
                return True
            cursors[category] = index
            return False

        # (1) every medical-device required question
        while len(chosen) < num_questions and take("md_required"):
            pass
        # (2) at least one safety question when warnings exist
        if len(chosen) < num_questions:
            take("safety")
        # (3) at least one sequence question when the procedure has >= 3 steps
        if len(chosen) < num_questions:
            take("sequence")

        # (4-6) top up: step content, purpose/scope, definitions
        cycle_index = 0
        while len(chosen) < num_questions:
            category = self.FILL_CYCLE[cycle_index % len(self.FILL_CYCLE)]
            cycle_index += 1
            if take(category):
                continue
            if all(cursors[key] >= len(buckets[key]) for key in buckets):
                break
        if shared:
            self._note(
                "{0} candidate question(s) were not used because their answer "
                "turns on text another selected question already asks about - "
                "answering one would have answered the other.".format(shared))
        return chosen

    # -- pool ---------------------------------------------------------------
    def _rebalance_length_extremes(self, chosen: List[_Candidate],
                                   pool: List[_Candidate],
                                   doc_key: str = "") -> List[_Candidate]:
        """Stop "always click the longest option" from being a viable strategy.

        ``pick_distractors`` already straddles the correct answer's length, but a
        document can simply contain nothing longer (or nothing shorter) than a
        given correct answer, and in a five-question quiz two or three such
        questions are enough to matter.  Question *position* cannot fix this -
        the longest option is the longest wherever it sits - so the lever here is
        *which* questions are asked: a selected question whose correct option is
        a length extreme is swapped for an unused candidate from the SAME
        category that is not.  Category counts are untouched, so the coverage
        blueprint still holds.

        Best effort and bounded: a thin document may have no better-shaped
        alternative, and the naive-learner tests are what actually hold the line.
        """
        selected = {cand.qid for cand in chosen}
        spare: Dict[str, List[_Candidate]] = {}
        for candidate in pool:
            if candidate.qid not in selected and candidate.kind != "tf":
                spare.setdefault(candidate.category, []).append(candidate)

        for extreme in (0, 1):          # 0 = longest, 1 = shortest
            for _ in range(MAX_LENGTH_REPAIR_SWAPS):
                # Scaled points, to match _length_tell_weight's numerator.
                total = sum(_candidate_points(cand) for cand in chosen) or 1
                if _length_tell_weight(chosen, extreme) <= LENGTH_TELL_CEILING * total:
                    break

                offenders = sorted(
                    (c for c in chosen
                     if c.kind != "tf" and _length_extremes(c)[extreme]),
                    key=lambda c: (-_candidate_points(c), c.qid))
                if not offenders:
                    break

                swap = None
                # Prefer a replacement that is neither extreme; settle for one
                # that merely fixes the extreme being repaired.
                for strict in (True, False):
                    for offender in offenders:
                        remaining = [c for c in chosen if c is not offender]
                        for alternative in spare.get(offender.category, []):
                            flags = _length_extremes(alternative)
                            if strict and any(flags):
                                continue
                            if flags[extreme]:
                                continue
                            # A better-shaped question is no use if it shares its
                            # material with one already selected (see
                            # _select_by_blueprint).
                            if not self._keeps_questions_independent(
                                    remaining, alternative, doc_key):
                                continue
                            swap = (offender, alternative)
                            break
                        if swap:
                            break
                    if swap:
                        break
                if not swap:
                    break

                offender, alternative = swap
                chosen[chosen.index(offender)] = alternative
                spare[offender.category].remove(alternative)
                spare.setdefault(offender.category, []).append(offender)
        return chosen

    def _repair_cross_question_leakage(self, chosen: List[_Candidate],
                                       doc_key: str) -> int:
        """Stop one question's answer from being another question's distractor.

        The defect: a step question's correct answer (the Step 6 body) was also
        offered as a wrong answer in the Step 1 question.  A learner who knows
        one of them eliminates an option in the other without reading anything -
        the same free information the naive-learner work exists to remove.

        Every selected question's distractors are rebuilt from its own recorded
        source material with the other questions' answer texts taken out, so the
        length-straddling and register rules still apply (it is the same
        ``pick_distractors`` call with a smaller pool, seeded identically, so the
        result stays deterministic).  A question is allowed to lose an option
        this way - two options are weighted like a true/false item, see
        ``scale_points_for_options`` - because a smaller honest question beats a
        bigger one a learner can shortcut.

        Returns the number of distractors that STILL repeat another question's
        answer, which is only ever non-zero on a document with nothing else to
        offer.  ``generate()`` exposes it as ``Assessment.leakage_count`` and
        records a note; it is 0 on every bundled and gallery document.
        """
        leaks = 0
        for cand in chosen:
            if cand.kind == "tf":
                continue
            forbidden = self._forbidden_for(cand, chosen)
            if not forbidden:
                continue
            leaking = [d for d in cand.distractors
                       if related_texts(d) & forbidden]
            if not leaking:
                continue

            clean = [text for text in cand.sources
                     if not (related_texts(text) & forbidden)]
            rng = Random(seed_from_digest(question_digest(doc_key, cand.qid)))
            replacement = pick_distractors(cand.correct, clean, rng,
                                           limit=len(cand.distractors))
            if len(replacement) >= cand.min_distractors:
                cand.distractors = replacement
                continue
            # Nothing left to ask with: keep the question, keep the leak, and
            # report it rather than quietly shipping a shorter quiz.
            leaks += len(leaking)
        return leaks

    def _build_pool(self, sop_content: SOPContent, doc_key: str) -> List[_Candidate]:
        """Every question this document can honestly support, in a stable order."""
        pool: List[_Candidate] = []
        pool.extend(self._purpose_scope_candidates(sop_content, doc_key))
        pool.extend(self._definition_candidates(sop_content, doc_key))
        pool.extend(self._step_candidates(sop_content, doc_key))
        pool.extend(self._sequence_candidates(sop_content, doc_key))
        pool.extend(self._safety_candidates(sop_content, doc_key))
        return pool

    # -- shared material ----------------------------------------------------
    def _document_material(self, sop_content: SOPContent) -> Dict[str, List[str]]:
        """Reusable distractor material, all of it verbatim from the SOP."""
        steps = ordered_steps(getattr(sop_content, "procedures", None))
        return {
            "purpose": _sentences(getattr(sop_content, "purpose", "")),
            "scope": _sentences(getattr(sop_content, "scope", "")),
            "responsibilities": [
                _clean(r) for r in (getattr(sop_content, "responsibilities", None) or [])
                if len(_clean(r)) >= 15
            ],
            "definitions": [
                _clean(v) for v in (getattr(sop_content, "definitions", None) or {}).values()
                if len(_clean(v)) >= 15
            ],
            "step_answers": [_step_answer_text(s) for s in steps],
            "step_titles": [_step_option_text(s) for s in steps],
        }

    # -- purpose / scope ----------------------------------------------------
    def _prose_distractor_material(self, material: Dict[str, List[str]],
                                   own_key: str) -> List[str]:
        """Same-register material for a purpose or scope question.

        Step bodies are excluded *by construction*.  They are instructions, and
        a purpose statement offered against three instructions is answerable
        from the register alone - the defect this fixes.  What is left is prose
        about the procedure: the other section verbatim (true of that section,
        so a genuinely wrong answer here), responsibility statements,
        definitions, and honestly *altered* forms of the purpose and scope
        sentences, which read exactly like the real thing but are false.

        The section under test is never offered verbatim: another sentence of
        the PURPOSE section is *also* a statement of purpose, so it would not be
        a wrong answer.
        """
        other_key = "scope" if own_key == "purpose" else "purpose"
        verbatim = (material[other_key] + material["responsibilities"]
                    + material["definitions"])
        altered = [alter_statement(text) for text in
                   material[own_key] + material[other_key]]
        return verbatim + [text for text in altered if text]

    def _purpose_scope_candidates(self, sop_content, doc_key) -> List[_Candidate]:
        material = self._document_material(sop_content)
        purpose = material["purpose"]
        scope = material["scope"]
        multiple_choice: List[_Candidate] = []
        true_false: List[_Candidate] = []

        if purpose:
            qid = "purpose_mc_1"
            rng = Random(seed_from_digest(question_digest(doc_key, qid)))
            sources = same_register(
                purpose[0], self._prose_distractor_material(material, "purpose"))
            distractors = pick_distractors(purpose[0], sources, rng)
            if len(distractors) >= MIN_REGISTER_DISTRACTORS:
                multiple_choice.append(_Candidate.choice(
                    "purpose_scope", 0, qid,
                    "What is the primary purpose of this procedure, as stated in the SOP?",
                    clip_option(purpose[0]), distractors,
                    "The SOP purpose section states: {0}".format(purpose[0]),
                    points=POINTS_CHOICE, topic="Purpose",
                    source_ref={"kind": "purpose", "section": "PURPOSE"},
                    sources=sources,
                    min_distractors=MIN_REGISTER_DISTRACTORS,
                ))
            else:
                self._note(
                    "No multiple-choice purpose question: the SOP offers only "
                    "{0} statement(s) about the procedure that could serve as a "
                    "wrong answer, and a purpose statement shown against step "
                    "instructions is identifiable without reading the SOP. "
                    "Add scope, responsibility or definition text to enable "
                    "it.".format(len(distractors)))

        if scope:
            qid = "scope_mc_1"
            rng = Random(seed_from_digest(question_digest(doc_key, qid)))
            sources = same_register(
                scope[0], self._prose_distractor_material(material, "scope"))
            distractors = pick_distractors(scope[0], sources, rng)
            if len(distractors) >= MIN_REGISTER_DISTRACTORS:
                multiple_choice.append(_Candidate.choice(
                    "purpose_scope", 1, qid,
                    "According to the SOP, where and to whom does this procedure apply?",
                    clip_option(scope[0]), distractors,
                    "The SOP scope section states: {0}".format(scope[0]),
                    points=POINTS_CHOICE, topic="Scope",
                    source_ref={"kind": "scope", "section": "SCOPE"},
                    sources=sources,
                    min_distractors=MIN_REGISTER_DISTRACTORS,
                ))
            else:
                self._note(
                    "No multiple-choice scope question: the SOP offers only "
                    "{0} statement(s) about the procedure that could serve as a "
                    "wrong answer, and a scope statement shown against step "
                    "instructions is identifiable without reading the "
                    "SOP.".format(len(distractors)))

            altered = alter_statement(scope[0])
            true_false.append(_Candidate.true_false(
                "purpose_scope", 2, "scope_tf_1",
                "True or False: this procedure applies as follows - {0}".format(
                    clip_option(scope[0])),
                ("True or False: this procedure applies as follows - {0}".format(
                    clip_option(altered)) if altered else None),
                "The SOP scope section states: {0}".format(scope[0]),
                "That statement contradicts the SOP scope section, which states: {0}".format(scope[0]),
                points=POINTS_TRUE_FALSE, topic="Scope",
                source_ref={"kind": "scope", "section": "SCOPE"},
                true_material=scope[0], false_material=altered or "",
            ))

        if purpose:
            altered = alter_statement(purpose[0])
            true_false.append(_Candidate.true_false(
                "purpose_scope", 3, "purpose_tf_1",
                "True or False: the SOP states this purpose - {0}".format(
                    clip_option(purpose[0])),
                ("True or False: the SOP states this purpose - {0}".format(
                    clip_option(altered)) if altered else None),
                "The SOP purpose section states: {0}".format(purpose[0]),
                "That statement contradicts the SOP purpose section, which states: {0}".format(purpose[0]),
                points=POINTS_TRUE_FALSE, topic="Purpose",
                source_ref={"kind": "purpose", "section": "PURPOSE"},
                true_material=purpose[0], false_material=altered or "",
            ))

        return _interleave(multiple_choice, true_false)

    # -- definitions --------------------------------------------------------
    def _definition_candidates(self, sop_content, doc_key) -> List[_Candidate]:
        definitions = getattr(sop_content, "definitions", None) or {}
        terms = [(_clean(t), _clean(d)) for t, d in definitions.items()
                 if _clean(t) and len(_clean(d)) >= 10]
        if not terms:
            return []

        material = self._document_material(sop_content)
        multiple_choice: List[_Candidate] = []
        true_false: List[_Candidate] = []

        for index, (term, definition) in enumerate(terms):
            others = [d for t, d in terms if t != term]

            qid = "def_mc_{0}".format(index + 1)
            rng = Random(seed_from_digest(question_digest(doc_key, qid)))
            # A definition is prose about the procedure; step instructions are
            # not, so they are only used when the document has nothing else.
            sources = register_preferred(
                definition,
                others + material["purpose"] + material["scope"]
                + material["step_answers"],
            )
            distractors = pick_distractors(definition, sources, rng)
            if len(distractors) >= MIN_OPTIONS - 1:
                multiple_choice.append(_Candidate.choice(
                    "definition", index, qid,
                    'As defined in this SOP, what does "{0}" mean?'.format(term),
                    clip_option(definition), distractors,
                    '"{0}" is defined as: {1}'.format(term, definition),
                    points=POINTS_CHOICE, topic="Definitions",
                    source_ref={"kind": "definition", "term": term},
                    sources=sources,
                ))

            if others:
                qid_tf = "def_tf_{0}".format(index + 1)
                rng_tf = Random(seed_from_digest(question_digest(doc_key, qid_tf)))
                wrong = rng_tf.choice(sorted(others))
                true_false.append(_Candidate.true_false(
                    "definition", index, qid_tf,
                    'True or False: in this SOP, "{0}" is defined as - {1}'.format(
                        term, clip_option(definition)),
                    'True or False: in this SOP, "{0}" is defined as - {1}'.format(
                        term, clip_option(wrong)),
                    '"{0}" is defined as: {1}'.format(term, definition),
                    'That is another term\'s definition. "{0}" is defined as: {1}'.format(
                        term, definition),
                    points=POINTS_TRUE_FALSE, topic="Definitions",
                    source_ref={"kind": "definition", "term": term},
                    true_material=definition, false_material=wrong,
                ))

        # Rotated: def_mc_1 is followed by def_tf_2, not by its own twin.
        return _interleave(multiple_choice, true_false, rotate=1)

    # -- step content -------------------------------------------------------
    def _step_candidates(self, sop_content, doc_key) -> List[_Candidate]:
        steps = ordered_steps(getattr(sop_content, "procedures", None))
        if len(steps) < 2:
            return []

        material = self._document_material(sop_content)
        candidates: List[_Candidate] = []

        # Midpoint-bisection order so a short quiz samples across the whole
        # procedure instead of clustering on the first few steps.
        for order, index in enumerate(_spread_indices(len(steps))):
            proc = steps[index]
            number = step_number(proc) or str(index + 1)
            title = step_title(proc)
            body = step_body(proc)

            if body:
                # Full step text available: ask what the step requires and use
                # other steps' instructions as distractors.
                prompt = 'According to Step {0} ("{1}"), what must be done?'.format(
                    number, title)
                correct = clip_option(body)
                sources = [_step_answer_text(s) for i, s in enumerate(steps) if i != index]
            else:
                # Heading-only parse: the title IS the answer, so it must not
                # appear in the prompt.
                prompt = "What action does Step {0} of this procedure require?".format(number)
                correct = clip_option(title)
                sources = [_step_option_text(s) for i, s in enumerate(steps) if i != index]

            # Other steps first: they are instructions, like the answer.
            # Definitions and scope prose are a different register and are only
            # used when the procedure is too short to supply enough steps.
            sources = register_preferred(
                correct, sources + material["definitions"] + material["scope"])
            qid = "step_mc_{0}".format(re.sub(r"[^0-9A-Za-z]+", "_", number) or str(index + 1))
            rng = Random(seed_from_digest(question_digest(doc_key, qid)))
            distractors = pick_distractors(correct, sources, rng)
            if len(distractors) < MIN_OPTIONS - 1:
                continue

            candidates.append(_Candidate.choice(
                "step", order, qid, prompt, correct, distractors,
                "Step {0} states: {1}".format(number, body or title),
                points=POINTS_CHOICE, topic="Step {0}".format(number),
                source_ref={"kind": "step", "step_number": number,
                            "source_lines": proc.get("source_lines")},
                sources=sources,
            ))
        return candidates

    # -- step sequence ------------------------------------------------------
    def _sequence_candidates(self, sop_content, doc_key) -> List[_Candidate]:
        """Replacement for the broken ``ordering`` question type.

        The old type put a list of step numbers in ``correct_answer`` while the
        renderer compared a single radio value against it, so it could never be
        answered correctly.  This asks which action comes immediately after a
        given step and offers other steps' *titles* - numbers stripped, so the
        answer cannot be read off the labels.
        """
        steps = ordered_steps(getattr(sop_content, "procedures", None))
        if len(steps) < 3:
            return []

        candidates: List[_Candidate] = []
        positions = [i for i in _spread_indices(len(steps)) if i < len(steps) - 1]
        for order, index in enumerate(positions):
            current, following = steps[index], steps[index + 1]
            number = step_number(current) or str(index + 1)
            next_number = step_number(following) or str(index + 2)
            correct = _step_option_text(following)
            if not correct:
                continue

            sources = [_step_option_text(s) for i, s in enumerate(steps)
                       if i not in (index, index + 1)]
            qid = "seq_{0}".format(re.sub(r"[^0-9A-Za-z]+", "_", number) or str(index + 1))
            rng = Random(seed_from_digest(question_digest(doc_key, qid)))
            distractors = pick_distractors(correct, sources, rng)
            if not distractors:
                continue
            if len(distractors) < 2:
                # Short procedure: one plausible meta-option rather than filler.
                ending = "No further action - the procedure ends here."
                distractors = distractors + [ending]
                sources = sources + [ending]

            candidates.append(_Candidate.choice(
                "sequence", order, qid,
                'Which action is performed immediately after Step {0} ("{1}")?'.format(
                    number, step_title(current)),
                correct, distractors,
                "Step {0} is followed by Step {1}: {2}".format(
                    number, next_number, step_title(following)),
                kind="sequence",
                points=POINTS_CHOICE, topic="Step sequence",
                source_ref={"kind": "sequence", "after_step": number,
                            "answer_step": next_number},
                sources=sources,
            ))
        return candidates

    # -- safety -------------------------------------------------------------
    def _safety_candidates(self, sop_content, doc_key) -> List[_Candidate]:
        warnings: List[str] = []
        seen = set()
        for raw in (getattr(sop_content, "safety_warnings", None) or []):
            warning = _clean(raw)
            norm = normalize_option_text(warning)
            if len(warning) < 15 or norm in seen:
                continue
            seen.add(norm)
            warnings.append(warning)
        if not warnings:
            return []

        altered = [alter_statement(w) for w in warnings]
        multiple_choice: List[_Candidate] = []
        true_false: List[_Candidate] = []

        for index, warning in enumerate(warnings):
            # "Which of these is a warning stated in this SOP?"  Distractors are
            # altered forms of the *other* real warnings - never another real
            # warning, which would make two options correct.
            sources = [a for j, a in enumerate(altered) if j != index and a]
            if sources:
                qid = "safety_mc_{0}".format(index + 1)
                rng = Random(seed_from_digest(question_digest(doc_key, qid)))
                distractors = pick_distractors(warning, sources, rng)
                if len(distractors) >= MIN_OPTIONS - 1:
                    multiple_choice.append(_Candidate.choice(
                        "safety", index * 2, qid,
                        "Which of the following safety warnings is stated in this SOP?",
                        clip_option(warning), distractors,
                        "The SOP states: {0}".format(warning),
                        points=POINTS_SAFETY_CHOICE, topic="Safety warnings",
                        source_ref={"kind": "safety", "warning_index": index},
                        sources=sources,
                    ))

            true_false.append(_Candidate.true_false(
                "safety", index * 2 + 1, "safety_tf_{0}".format(index + 1),
                "True or False: this SOP requires the following - {0}".format(
                    clip_option(warning)),
                ("True or False: this SOP requires the following - {0}".format(
                    clip_option(altered[index])) if altered[index] else None),
                "The SOP states: {0}".format(warning),
                "That contradicts the SOP, which states: {0}".format(warning),
                points=POINTS_SAFETY_TRUE_FALSE, topic="Safety warnings",
                source_ref={"kind": "safety", "warning_index": index},
                true_material=warning, false_material=altered[index] or "",
            ))

        # "Which statement contradicts a warning?" - correct answer is the
        # altered warning, distractors are real warnings.  Needs >= 3 warnings so
        # there are two genuine non-contradicting options.
        if len(warnings) >= 3:
            for index, alt in enumerate(altered[:2]):
                if not alt:
                    continue
                qid = "safety_neg_{0}".format(index + 1)
                rng = Random(seed_from_digest(question_digest(doc_key, qid)))
                sources = [w for j, w in enumerate(warnings) if j != index]
                distractors = pick_distractors(alt, sources, rng)
                if len(distractors) >= 2:
                    multiple_choice.append(_Candidate.choice(
                        "safety", 100 + index, qid,
                        "Which of the following statements CONTRADICTS a safety "
                        "warning in this SOP?",
                        clip_option(alt), distractors,
                        "The SOP states: {0}".format(warnings[index]),
                        points=POINTS_SAFETY_CHOICE, topic="Safety warnings",
                        source_ref={"kind": "safety_contradiction",
                                    "warning_index": index},
                        sources=sources,
                        min_distractors=2,
                    ))

        # Rotated: safety_mc_1 is followed by safety_tf_2, not by its own twin.
        return _interleave(multiple_choice, true_false, rotate=1)

    # -- legacy -------------------------------------------------------------
    def _load_question_templates(self) -> Dict:
        """Question phrasing reference.

        Retained for backward compatibility with callers that inspect it; the
        generator builds its prompts inline so each one can name the step,
        term or section it is about.
        """
        return {
            "purpose": [
                "What is the primary purpose of this procedure, as stated in the SOP?",
            ],
            "scope": [
                "According to the SOP, where and to whom does this procedure apply?",
            ],
            "procedure": [
                "According to Step {step_num}, what must be done?",
                'Which action is performed immediately after Step {step_num}?',
            ],
            "safety": [
                "Which of the following safety warnings is stated in this SOP?",
                "Which of the following statements CONTRADICTS a safety warning in this SOP?",
            ],
            "definition": [
                'As defined in this SOP, what does "{term}" mean?',
            ],
        }


class MedicalDeviceAssessmentGenerator(AssessmentGenerator):
    """Assessment generator specifically for medical device training"""

    def __init__(self):
        super().__init__()
        self.min_passing = 80

    def generate(self, sop_content, num_questions=5, passing_score=80):
        """
        Generate assessment with medical device specific questions.

        ``num_questions`` is the TOTAL, including the two required compliance
        questions.  The old implementation asked the base generator for
        ``num_questions - 2`` and then appended two more, but the base only
        trimmed when the pool was larger than the request, so the returned count
        rarely matched what was asked for.  The required questions are now part
        of the candidate pool and sit at the top of the coverage blueprint, so
        the count is exact and they are never crowded out.

        Args:
            sop_content: Parsed SOP content
            num_questions: Total number of questions
            passing_score: Minimum passing score (raised to 80 if lower)

        Returns:
            Assessment object with medical device compliance questions
        """
        assessment = super().generate(
            sop_content, num_questions, max(int(passing_score), self.min_passing)
        )
        if assessment.passing_score < self.min_passing:
            assessment.passing_score = self.min_passing
        assessment.description = (
            "Complete this assessment to verify your understanding of the procedure. "
            "This assessment includes compliance questions required for medical "
            "device manufacturing."
        )
        return assessment

    def _build_pool(self, sop_content, doc_key):
        return self._required_medical_candidates() + super()._build_pool(
            sop_content, doc_key)

    def _required_medical_candidates(self) -> List[_Candidate]:
        """Questions FDA expects to see in all medical device training.

        Sourced from ``MEDICAL_DEVICE_CONFIG['required_questions']`` so the
        wording lives in one place.  The distractors there are plausible
        real-world wrong behaviours (proceed on judgement, follow the version you
        were trained on, ask a colleague) rather than self-evidently absurd
        options.

        1. Deviation handling per 21 CFR 820.70
        2. Impact awareness per 21 CFR 820.25
        """
        from .medical_device_config import MEDICAL_DEVICE_CONFIG

        candidates: List[_Candidate] = []
        for index, spec in enumerate(MEDICAL_DEVICE_CONFIG.get("required_questions", [])):
            qid = spec.get("id") or "md_req_{0}".format(index + 1)
            if spec.get("type") == "true_false":
                candidates.append(_Candidate.true_false(
                    "md_required", index, qid,
                    spec["text"],
                    None,  # a compliance statement whose answer must be True
                    spec.get("explanation", ""),
                    "",
                    points=spec.get("points", 2),
                    topic="Regulatory compliance",
                    source_ref={"kind": "md_required",
                                "reference": spec.get("reference", "")},
                ))
            else:
                candidates.append(_Candidate.choice(
                    "md_required", index, qid,
                    spec["text"],
                    clip_option(spec["correct"]),
                    [clip_option(d) for d in spec.get("distractors", [])],
                    spec.get("explanation", ""),
                    points=spec.get("points", 2),
                    topic="Regulatory compliance",
                    source_ref={"kind": "md_required",
                                "reference": spec.get("reference", "")},
                ))
        return candidates

    def _add_required_medical_questions(self) -> List[Question]:
        """Backward-compatible helper returning the required questions as
        ``Question`` objects in canonical (unshuffled) order.

        ``generate()`` no longer calls this - the required questions enter
        through the candidate pool so the blueprint can order and shuffle them
        with everything else.
        """
        doc_key = document_key("", "")
        return [cand.materialize(doc_key, True)
                for cand in self._required_medical_candidates()]
