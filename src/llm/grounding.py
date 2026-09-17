"""The automated grounding check: does the document actually say this?

This module is the reason the LLM layer is allowed to exist at all.  GOAL.md's
M1 requires that "every generated sentence carries a citation to the source
line, and an automated check rejects unsupported claims", and Risk 2 says the
product is *acceleration with a human in the loop*, never automation of a
regulated judgment.  So: nothing a model writes reaches a learner unless it
survives :func:`verify_claim` (or, for wrong answers, :func:`verify_distractor`),
and everything that survives still goes to an SME for approval.

The check is deliberately mechanical and deliberately strict.  It is not a
semantic entailment judge - it cannot be, because a second model grading the
first one is exactly the black box QA/RA will not accept.  It is four cheap,
explainable tests that a reviewer can re-run by eye:

1. **The citation resolves.**  The span the model gave must be real line
   numbers inside the document it was shown.
2. **No invented numbers.**  Every digit-bearing token in the sentence (a
   count, a limit, a duration, a form number, a channel) must appear in the
   cited excerpt.  This is the single highest-value test: a hallucinated "30
   minutes" in a regulated procedure is the failure mode that ends the company.
3. **The wording comes from the source.**  At least ``min_overlap`` of the
   sentence's content words appear in the excerpt - or, for a terse sentence
   built around named things, every capitalised or defined term does.
4. **Shape.**  Non-empty, and no longer than ``max_sentence_chars``.

False (distractor) options invert test 3: a wrong answer that the document
*asserts* is not a distractor, it is a broken question, and it is rejected.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..answer_key import normalize_option_text
from ..assessments import MAX_OPTION_CHARS, NEAR_IDENTICAL_RATIO, clip_option
from .config import LLMConfig

#: Words carrying no evidential weight.  Kept small and boring on purpose: a
#: bigger list makes the overlap test look stricter than it is.
STOPWORDS = frozenset("""
a an and are as at be been before being between both but by can cannot could
did do does doing done during each either for from further had has have having
he her here hers him his how i if in into is it its itself just may me might
more most must my no nor not of off on once only or other our ours out over
own same she should so some such than that the their theirs them themselves
then there these they this those through to too under until up upon very was
we were what when where which while who whom why will with within would you
your yours
""".split())

#: A digit-bearing run: 5, 5.5, 1,200, 30%.  Any of these appearing in a
#: generated sentence but not in the cited excerpt is an invented number.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")

#: Words that carry the polarity / modality of a requirement.  Two sentences
#: that share most of their content words but differ here are a statement and
#: its contradiction - which is precisely what a good distractor is.
#: (Normalisation strips punctuation, so contractions are not listed - "don't"
#: reduces to "don" + "t" and would never match.)
POLARITY_MARKERS = frozenset("""
after all always any before cannot every immediately least mandatory may most
must never no none not only optional permitted prohibited required shall some
without
""".split())

_PUNCT_RE = re.compile(r"[^\w\s%]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

#: A distractor shorter than this cannot meaningfully be "contained in the
#: document" - almost any short phrase is.  Below it we rely on the
#: near-identity and overlap tests only.
_MIN_CONTAINMENT_CHARS = 20


@dataclass
class Verdict:
    """Outcome of one grounding check.

    ``reasons`` is populated on failure *and* carries the supporting detail on
    success, because the transparency report shows both to the SME.
    """

    ok: bool
    reasons: List[str] = field(default_factory=list)
    #: Diagnostics worth showing a reviewer (overlap achieved, excerpt used).
    detail: Dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> Dict:
        return {"ok": self.ok, "reasons": list(self.reasons),
                "detail": dict(self.detail)}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def normalize(text) -> str:
    """NFKC, casefold, punctuation to spaces, whitespace collapsed."""
    if text is None:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", folded)).strip()


def content_words(text) -> List[str]:
    """Normalised tokens with the stopwords removed, order preserved."""
    return [w for w in normalize(text).split() if w and w not in STOPWORDS]


def numeric_tokens(text) -> Set[str]:
    """Digit-bearing tokens, with thousands separators removed.

    ``1,200`` and ``1200`` are the same number; ``MS-101`` contributes ``101``,
    which is enough to catch an invented form number.
    """
    return {match.group(0).replace(",", "")
            for match in _NUMBER_RE.finditer(str(text or ""))}


def capitalised_terms(text) -> Set[str]:
    """Proper nouns and acronyms, normalised.

    The first word of the sentence is skipped - it is capitalised by grammar,
    not by being a named thing.
    """
    words = re.findall(r"[A-Za-z][\w/\-]*", str(text or ""))
    terms = set()
    for index, word in enumerate(words):
        if index == 0:
            continue
        if word.isupper() and len(word) > 1:
            terms.add(normalize(word))
        elif word[0].isupper():
            terms.add(normalize(word))
    return {t for t in terms if t and t not in STOPWORDS}


def polarity_signature(text) -> frozenset:
    """The polarity / modality words present, as a set."""
    return frozenset(w for w in normalize(text).split() if w in POLARITY_MARKERS)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Document access
#
# Tolerant by design.  Today ``SOPContent`` has ``raw_content``; a concurrent
# stream is adding ``lines`` (and per-field ``provenance``).  Use the richer
# shape when it is there, fall back when it is not, and never require it.
# ---------------------------------------------------------------------------
def document_lines(sop) -> List[str]:
    """The document as a list of lines, 0-indexed here, 1-indexed in citations."""
    lines = getattr(sop, "lines", None)
    if isinstance(lines, (list, tuple)) and lines:
        return [str(line) for line in lines]
    raw = getattr(sop, "raw_content", "") or ""
    return raw.splitlines()


def resolve_span(sop, span) -> Optional[Tuple[int, int]]:
    """Normalise a ``[start, end]`` 1-indexed citation, or None if unusable."""
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None
    try:
        start, end = int(span[0]), int(span[1])
    except (TypeError, ValueError):
        return None
    if start > end:
        start, end = end, start
    total = len(document_lines(sop))
    if total == 0:
        return None
    if start < 1 or end > total:
        return None
    return start, end


def span_excerpt(sop, span, context: int = 1) -> str:
    """The cited lines plus ``context`` lines either side, joined by newlines.

    Returns "" when the span does not resolve.  The context window is what
    makes the number test fair: a parser-level line break should not turn a
    faithfully quoted figure into an "invented" one.
    """
    resolved = resolve_span(sop, span)
    if resolved is None:
        return ""
    start, end = resolved
    lines = document_lines(sop)
    lo = max(0, start - 1 - max(0, context))
    hi = min(len(lines), end + max(0, context))
    return "\n".join(lines[lo:hi])


def document_sentences(sop) -> List[str]:
    """Every sentence in the document, for the distractor assertion test."""
    out: List[str] = []
    for line in document_lines(sop):
        text = line.strip()
        if not text:
            continue
        for part in _SENTENCE_SPLIT_RE.split(text):
            part = part.strip()
            if part:
                out.append(part)
    return out


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def verify_claim(sentence, sop, span,
                 config: Optional[LLMConfig] = None) -> Verdict:
    """Is ``sentence`` supported by the document at ``span``?

    Returns a :class:`Verdict`; ``reasons`` names every test that failed, so a
    rejection is explainable in the SME report rather than a bare "no".
    """
    cfg = config or LLMConfig()
    reasons: List[str] = []
    detail: Dict = {}

    text = ("" if sentence is None else str(sentence)).strip()

    # (4) shape
    if not text:
        return Verdict(False, ["The generated sentence is empty."], detail)
    if len(text) > cfg.max_sentence_chars:
        reasons.append(
            "The sentence is {0} characters; the limit is {1}.".format(
                len(text), cfg.max_sentence_chars))

    # (1) the citation resolves
    resolved = resolve_span(sop, span)
    if resolved is None:
        total = len(document_lines(sop))
        reasons.append(
            "The cited span {0!r} does not resolve to lines 1-{1} of the "
            "document.".format(span, total))
        return Verdict(False, reasons, detail)
    detail["span"] = [resolved[0], resolved[1]]

    excerpt = span_excerpt(sop, span, cfg.span_context_lines)
    detail["excerpt"] = excerpt

    # (2) no invented numbers
    missing_numbers = sorted(numeric_tokens(text) - numeric_tokens(excerpt))
    if missing_numbers:
        reasons.append(
            "These numbers do not appear in the cited lines: {0}.".format(
                ", ".join(missing_numbers)))

    # (3) the wording comes from the source
    sentence_words = content_words(text)
    excerpt_words = set(content_words(excerpt))
    if not sentence_words:
        reasons.append("The sentence carries no content words to check.")
        overlap = 0.0
    else:
        hits = sum(1 for w in sentence_words if w in excerpt_words)
        overlap = hits / float(len(sentence_words))
    detail["content_word_overlap"] = round(overlap, 4)

    terms = capitalised_terms(text)
    missing_terms = sorted(t for t in terms if t not in excerpt_words)
    detail["named_terms"] = sorted(terms)

    terms_cover = bool(terms) and not missing_terms
    if overlap < cfg.min_overlap and not terms_cover:
        reasons.append(
            "Only {0:.0%} of the sentence's content words appear in the cited "
            "lines (minimum {1:.0%}){2}.".format(
                overlap, cfg.min_overlap,
                "; unsupported terms: " + ", ".join(missing_terms)
                if missing_terms else ""))

    return Verdict(not reasons, reasons, detail)


def document_asserts(text, sop) -> Optional[str]:
    """Does the document itself state ``text``?  Returns the reason, or None.

    Three escalating tests, all cheap and all explainable:

    * the normalised sentence appears verbatim inside the normalised document;
    * it is >= NEAR_IDENTICAL_RATIO similar to some document sentence (the same
      bar :func:`src.assessments.pick_distractors` uses to throw out a
      distractor that is really the correct answer in disguise);
    * it shares >= 85% of its content words with a document sentence *and* has
      the same polarity signature.  The polarity clause is what separates
      "Never restart without an inspection" (asserted) from "Always restart
      immediately without an inspection" (contradicted, and therefore a good
      distractor) - those two sentences share most of their words.
    """
    candidate = normalize(text)
    if not candidate:
        return "The distractor is empty."

    if len(candidate) >= _MIN_CONTAINMENT_CHARS:
        whole = normalize("\n".join(document_lines(sop)))
        if candidate in whole:
            return ("The document states this verbatim, so it is not a wrong "
                    "answer - offering it makes the question unanswerable.")

    candidate_words = content_words(text)
    candidate_polarity = polarity_signature(text)
    if not candidate_words:
        return None

    for sentence in document_sentences(sop):
        normalised = normalize(sentence)
        if not normalised:
            continue
        if similarity(candidate, normalised) >= NEAR_IDENTICAL_RATIO:
            return ("The document says almost exactly this ({0!r}), so it is "
                    "not a wrong answer.".format(_snip(sentence)))
        sentence_words = set(content_words(sentence))
        hits = sum(1 for w in candidate_words if w in sentence_words)
        overlap = hits / float(len(candidate_words))
        if overlap >= 0.85 and polarity_signature(sentence) == candidate_polarity:
            return ("The document asserts the same thing in the same terms "
                    "({0!r}), so it is not a wrong answer.".format(
                        _snip(sentence)))
    return None


def verify_distractor(text, correct, sop,
                      config: Optional[LLMConfig] = None) -> Verdict:
    """Is ``text`` usable as a *wrong* option next to ``correct``?

    Two independent requirements:

    * it must be distinguishable from the correct answer.  Same conventions as
      :func:`src.assessments.pick_distractors`: not empty, not a duplicate, not
      containing or contained by the correct answer, and below
      NEAR_IDENTICAL_RATIO similarity to it.  A learner must not be able to
      argue that two options were both right.
    * it must be false *with respect to this document*.  A "distractor" the SOP
      actually asserts is a broken question, not a hard one, and this is the
      test that catches a model that reached for a plausible-sounding real
      warning instead of inventing a contradiction.
    """
    cfg = config or LLMConfig()
    reasons: List[str] = []
    detail: Dict = {}

    candidate = clip_option(text)
    if not candidate:
        return Verdict(False, ["The proposed distractor is empty."], detail)
    if len(str(text).strip()) > MAX_OPTION_CHARS:
        reasons.append(
            "The proposed distractor is {0} characters; options are capped at "
            "{1}.".format(len(str(text).strip()), MAX_OPTION_CHARS))

    candidate_norm = normalize_option_text(candidate)
    correct_norm = normalize_option_text(clip_option(correct))
    detail["normalised"] = candidate_norm

    if not candidate_norm:
        return Verdict(False, ["The proposed distractor normalises to nothing."],
                       detail)
    if not correct_norm:
        return Verdict(False, ["The question has no usable correct option text."],
                       detail)

    if candidate_norm == correct_norm:
        reasons.append("It is the correct answer.")
    elif candidate_norm in correct_norm or correct_norm in candidate_norm:
        reasons.append("It contains, or is contained by, the correct answer.")
    else:
        ratio = similarity(candidate_norm, correct_norm)
        detail["similarity_to_correct"] = round(ratio, 4)
        if ratio >= NEAR_IDENTICAL_RATIO:
            reasons.append(
                "It is {0:.0%} similar to the correct answer (limit "
                "{1:.0%}).".format(ratio, NEAR_IDENTICAL_RATIO))

    asserted = document_asserts(candidate, sop)
    if asserted:
        reasons.append(asserted)

    return Verdict(not reasons, reasons, detail)


def _snip(text, limit: int = 80) -> str:
    text = _WS_RE.sub(" ", str(text)).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def split_sentences(text) -> List[str]:
    """Split generated prose into sentences for per-sentence verification."""
    cleaned = _WS_RE.sub(" ", str(text or "")).strip()
    if not cleaned:
        return []
    return [p.strip() for p in _SENTENCE_SPLIT_RE.split(cleaned) if p.strip()]


def coverage(verdicts: Iterable[Verdict]) -> Tuple[int, int]:
    """(accepted, total) over a run of verdicts - used for the report."""
    items: Sequence[Verdict] = list(verdicts)
    return sum(1 for v in items if v.ok), len(items)
