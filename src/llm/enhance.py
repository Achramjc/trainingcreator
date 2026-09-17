"""Apply grounded LLM enhancement to a TrainingModule and an Assessment.

Three tasks, one model call each, all optional and all individually revocable:

a. **objectives**   - rewrite each deterministic objective as a Bloom's-aligned,
   SME-quality objective carrying a cited span.
b. **section summaries** - a 2-4 sentence plain-language summary per section,
   every sentence cited, injected at the top of the section as an escaped
   ``<p class="summary">``.
c. **distractors** - replacements for weak wrong answers on multiple-choice
   questions only.  The correct option is never touched.

Five invariants hold no matter what the model returns:

* **Nothing hostile-shaped is used, whatever its citation says.**  Every item is
  put through :func:`output_filter_reasons` *before* the grounding check: a URL,
  an e-mail address, a phone-number-like pattern, an instruction-to-AI phrase, a
  role marker, markup, hidden characters, or a mention of instructions/prompts/AI
  the cited lines do not contain, and the item is dropped with kind
  ``output_filter``.  This layer exists because grounding cannot help here - an
  injected sentence really is in the document, so repeating it really is
  grounded (``docs/SECURITY.md``).

* **Inputs are never mutated.**  Everything works on a ``deepcopy`` and the
  original object is returned unchanged if anything goes wrong.  The
  deterministic pipeline's byte-identical-build property is untouched.
* **Off means off.**  With ``TRAINING_CREATOR_LLM=off``, a ``NullProvider``, or
  no provider at all, the functions return inputs whose ``to_dict()`` compares
  equal to the input's, plus an empty report.
* **A failure is not an exception.**  A provider that raises, errors, refuses,
  or returns nonsense yields the deterministic content and a report that says
  why.  The pipeline always completes.
* **M0's assessment invariants are re-proved, not assumed.**  Swapping
  distractors changes option lengths, and "always click the longest option" is
  a strategy M0 defeated by *shaping* those lengths.  So after any distractor
  change the answer positions are re-assigned and every naive strategy is
  re-scored; if one of them would now reach the passing mark, the distractor
  changes are reverted wholesale and the report says so.
"""

import copy
import html
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..answer_key import document_key, normalize_option_text
from ..assessments import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    _assign_answer_positions,
    naive_strategies,
    strategy_pick,
)
from ..injection_scan import KIND_DESCRIPTIONS, KIND_REPETITION, scan_document
from .config import LLMConfig
from .grounding import (
    Verdict,
    resolve_span,
    span_excerpt,
    verify_claim,
    verify_distractor,
)
from .prompts import (
    DISTRACTORS_SCHEMA,
    OBJECTIVES_SCHEMA,
    SUMMARIES_SCHEMA,
    distractors_prompt,
    objectives_prompt,
    summaries_prompt,
    system_blocks,
    user_blocks,
)

TASK_OBJECTIVES = "objectives"
TASK_SUMMARIES = "section_summaries"
TASK_DISTRACTORS = "distractors"

#: Reason kind recorded when an item is dropped by the output filters below
#: rather than by the grounding check.  Named so the SME report can be read for
#: "did anything come back that looked like an injection succeeded?".
REASON_OUTPUT_FILTER = "output_filter"

#: Longest an enhanced objective may be.  Longer than this is a paragraph, and
#: an objective a learner cannot hold in their head is not an objective.
MAX_OBJECTIVE_CHARS = 200

#: How many sentences a section summary must carry to be usable.
MIN_SUMMARY_SENTENCES = 2
MAX_SUMMARY_SENTENCES = 4


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
@dataclass
class EnhancementItem:
    """One proposal and what happened to it.  Shown to the SME verbatim."""

    task: str
    target: str
    accepted: bool
    reason: str
    original: str = ""
    proposed: str = ""
    span: Optional[List[int]] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    #: What decided this item, when it was not the grounding check.  Currently
    #: only ``"output_filter"`` (see :func:`output_filter_reasons`); empty for
    #: everything else.  An SME or auditor reading the report can filter on it
    #: to answer "did anything come back that looked like an injection took?".
    kind: str = ""

    def to_dict(self) -> Dict:
        return {
            "task": self.task,
            "target": self.target,
            "accepted": self.accepted,
            "reason": self.reason,
            "original": self.original,
            "proposed": self.proposed,
            "span": list(self.span) if self.span else None,
            "detail": dict(self.detail),
            "kind": self.kind,
        }


class EnhancementReport:
    """What the LLM layer proposed, what was kept, and what it cost.

    This is an SME- and auditor-facing artifact.  It is written to
    ``enhancement_report.json`` by the CLI and is intended to be folded into
    ``src/transparency_report.py`` by the coordinator.  Two things it must
    always be able to say: *every* item the model proposed (accepted or not,
    with the reason), and the fact that nothing here is approved content until
    a named human approves it.
    """

    #: Restated on every report.  GOAL.md Risk 2: acceleration with a human in
    #: the loop, never automation of a regulated judgment.
    DISCLOSURE = (
        "Machine-assisted draft. Every item below was generated from the source "
        "document and mechanically checked against the cited lines; items that "
        "failed the check were discarded and the deterministic text kept. "
        "Passing the check is not approval. A named subject-matter expert must "
        "review and approve this content before any learner sees it."
    )

    def __init__(self, scope: str, config: Optional[LLMConfig] = None):
        self.scope = scope                       # "module" | "assessment"
        self.config = config or LLMConfig()
        self.enabled = self.config.enabled
        self.items: List[EnhancementItem] = []
        self.calls: List[Dict[str, Any]] = []
        self.notes: List[str] = list(self.config.notes)

    # -- recording ----------------------------------------------------------
    def record(self, task: str, target: str, accepted: bool, reason: str,
               original: str = "", proposed: str = "",
               span: Optional[Sequence[int]] = None,
               detail: Optional[Dict] = None,
               kind: str = "") -> EnhancementItem:
        item = EnhancementItem(
            task=task, target=str(target), accepted=bool(accepted),
            reason=reason, original=original, proposed=proposed,
            span=list(span) if span else None, detail=dict(detail or {}),
            kind=kind)
        self.items.append(item)
        return item

    def record_output_filter(self, task: str, target: str, reasons: Sequence[str],
                             original: str = "", proposed: str = "",
                             span: Optional[Sequence[int]] = None
                             ) -> EnhancementItem:
        """Record a rejection by the output filters, with kind ``output_filter``.

        Separate from :meth:`record_verdict` on purpose: an item dropped here was
        not unsupported, it was *hostile-shaped*, and those are different findings
        for whoever reads this report.
        """
        return self.record(
            task, target, False,
            "Rejected by the output filter (prompt-injection defence): "
            + " ".join(reasons),
            original=original, proposed=proposed, span=span,
            detail={"output_filter_reasons": list(reasons)},
            kind=REASON_OUTPUT_FILTER)

    @property
    def output_filtered(self) -> List[EnhancementItem]:
        """Items the output filters dropped.  Read by tests and by the report."""
        return [i for i in self.items if i.kind == REASON_OUTPUT_FILTER]

    def record_verdict(self, task: str, target: str, verdict: Verdict,
                       original: str = "", proposed: str = "",
                       span: Optional[Sequence[int]] = None,
                       accepted_reason: str = "Supported by the cited lines.") -> bool:
        self.record(task, target, verdict.ok,
                    accepted_reason if verdict.ok else " ".join(verdict.reasons),
                    original=original, proposed=proposed, span=span,
                    detail=verdict.detail)
        return verdict.ok

    def record_call(self, task: str, result) -> None:
        entry = {"task": task}
        entry.update(result.to_dict())
        self.calls.append(entry)

    def note(self, text: str) -> None:
        self.notes.append(text)

    # -- reading ------------------------------------------------------------
    @property
    def accepted(self) -> List[EnhancementItem]:
        return [i for i in self.items if i.accepted]

    @property
    def rejected(self) -> List[EnhancementItem]:
        return [i for i in self.items if not i.accepted]

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    def counts_by_task(self) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for item in self.items:
            bucket = out.setdefault(item.task, {"accepted": 0, "rejected": 0})
            bucket["accepted" if item.accepted else "rejected"] += 1
        return out

    def usage_totals(self) -> Dict[str, int]:
        totals: Dict[str, int] = {}
        for call in self.calls:
            for key, value in (call.get("usage") or {}).items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
        return totals

    @property
    def errors(self) -> List[str]:
        return [c["error"] for c in self.calls if c.get("error")]

    def to_dict(self) -> Dict:
        return {
            "scope": self.scope,
            "enabled": self.enabled,
            "disclosure": self.DISCLOSURE,
            "config": self.config.to_dict(),
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "output_filtered_count": len(self.output_filtered),
            "counts_by_task": self.counts_by_task(),
            "token_usage": self.usage_totals(),
            "calls": [dict(c) for c in self.calls],
            "items": [i.to_dict() for i in self.items],
            "notes": list(self.notes),
        }

    def summary_lines(self) -> List[str]:
        """Short human summary for the CLI."""
        if not self.enabled:
            return ["LLM enhancement: off (deterministic content unchanged)."]
        lines = ["LLM enhancement ({0}, model {1}): {2} accepted, {3} rejected.".format(
            self.scope, self.config.model, self.accepted_count, self.rejected_count)]
        for task, counts in sorted(self.counts_by_task().items()):
            lines.append("  {0}: {1} accepted, {2} rejected".format(
                task, counts["accepted"], counts["rejected"]))
        usage = self.usage_totals()
        if usage:
            lines.append("  tokens: input {0}, cache read {1}, output {2}".format(
                usage.get("input_tokens", 0),
                usage.get("cache_read_input_tokens", 0),
                usage.get("output_tokens", 0)))
        for error in self.errors:
            lines.append("  ! {0}".format(error))
        for note in self.notes:
            lines.append("  note: {0}".format(note))
        return lines


def merge_reports(scope: str, reports: Sequence[EnhancementReport],
                  config: Optional[LLMConfig] = None) -> EnhancementReport:
    """Fold several reports into one (the CLI writes a single JSON file)."""
    merged = EnhancementReport(scope, config or (reports[0].config if reports
                                                 else LLMConfig()))
    merged.notes = []
    for report in reports:
        merged.items.extend(report.items)
        merged.calls.extend(report.calls)
        for note in report.notes:
            if note not in merged.notes:
                merged.notes.append(note)
    return merged


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------
def _is_off(provider, config: LLMConfig) -> bool:
    return (provider is None
            or not config.enabled
            or getattr(provider, "name", "") == "null")


def _call(provider, report: EnhancementReport, task: str, sop,
          user_prompt: str, schema: Dict) -> Optional[Dict]:
    """One provider call.  Returns parsed data, or None with the reason logged.

    A provider that *raises* is a bug in the provider, not in the pipeline, and
    it must not take the build down with it - so it is caught here and recorded
    exactly like a returned error.

    The document goes in the user turn, fenced, not in a ``system`` block:
    :func:`src.llm.prompts.user_blocks` builds both blocks and puts the cache
    breakpoint on the document, so the prefix is still shared across the three
    calls.  ``docs/SECURITY.md`` says why the position matters.
    """
    try:
        result = provider.complete_json(system_blocks(),
                                        user_blocks(sop, user_prompt), schema)
    except Exception as exc:                  # noqa: BLE001 - see docstring
        report.calls.append({
            "task": task, "ok": False, "refused": False,
            "error": "The provider raised {0}: {1}".format(
                type(exc).__name__, exc),
            "usage": {},
        })
        return None
    report.record_call(task, result)
    return result.data if result.ok else None


def _as_list(data: Optional[Dict], key: str) -> List[Dict]:
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Output filters - the layer that assumes the prompt failed
# ---------------------------------------------------------------------------
# Prompt design asks the model not to repeat an instruction it found in the
# document.  These filters assume, every time, that it did anyway.  They are
# lexical, cheap and deterministic, and they run BEFORE the grounding check, for
# two reasons: an injected sentence is *genuinely* supported by the lines it
# cites (the text really is in the document), so grounding will not stop it; and
# a rejection recorded as ``output_filter`` tells the SME something different
# from "unsupported by the citation".
#
# A rejection only ever means "keep the deterministic text", so these fail
# closed on purpose.  A summary carrying a numeric range that looks like a phone
# number is a false positive whose whole cost is a slightly clunkier sentence.
_OUTPUT_PHONE_RES = (
    re.compile(r"\+\d[\d\s().-]{7,}\d"),
    re.compile(r"\(\d{3}\)\s*\d{3}[-.\s]?\d{4}"),
    re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),
    re.compile(r"\b(?:phone|tel|telephone|call|contact|dial|text|whatsapp)\b"
               r"[^.\n]{0,24}?\b\d{3}[-.\s]?\d{4}\b", re.IGNORECASE),
)

#: Words that mean the model is talking about its own situation rather than the
#: procedure.  Allowed only when the cited source lines use them too - "work
#: instructions" is ordinary SOP vocabulary, "ignore the instructions above" is
#: not.
_OUTPUT_META_WORDS = ("instruction", "instructions", "prompt", "prompts",
                      "assistant", "ai", "llm", "language model", "chatbot",
                      "system message")

#: Markup is rejected outright rather than escaped.  Everything generated here is
#: escaped before it enters HTML anyway (CLAUDE.md invariant, tested), so a model
#: emitting angle brackets into a learning objective is not a rendering problem -
#: it is a signal that something in the document steered the output, and the
#: deterministic original is better.
_OUTPUT_MARKUP_RE = re.compile(r"[<>]")


def output_filter_reasons(text, source_excerpt: str = "") -> List[str]:
    """Why this generated item must not be used, or ``[]`` if it may be.

    ``text`` is one objective, one summary sentence or one distractor.
    ``source_excerpt`` is the cited lines, used only for the meta-word test: a
    document that says "work instructions" may have that phrase back, a document
    that does not may not.

    The injection-shaped checks are delegated to :mod:`src.injection_scan`, so the
    scanner and the filter cannot disagree about what an instruction-to-AI phrase,
    a role marker, a prompt delimiter, a URL, an e-mail address, hidden text or
    markup looks like.  One definition, two places it is applied.
    """
    candidate = str(text or "")
    if not candidate.strip():
        return ["The item is empty."]

    reasons: List[str] = []
    scan = scan_document([candidate])
    for finding in scan.findings:
        if finding.kind == KIND_REPETITION:      # meaningless for one line
            continue
        reasons.append("{0}: {1}".format(
            finding.kind, KIND_DESCRIPTIONS.get(finding.kind, "flagged")))

    for pattern in _OUTPUT_PHONE_RES:
        if pattern.search(candidate):
            reasons.append(
                "phone_number: the item contains a telephone-number-like "
                "pattern, which training content drawn from a procedure has no "
                "reason to introduce.")
            break

    if _OUTPUT_MARKUP_RE.search(candidate):
        reasons.append(
            "markup: the item contains '<' or '>'. Generated training prose is "
            "plain text; markup here means the model was echoing the document's "
            "formatting or something worse.")

    excerpt_lower = str(source_excerpt or "").lower()
    meta_hits = [word for word in _OUTPUT_META_WORDS
                 if re.search(r"\b" + re.escape(word) + r"\b", candidate.lower())
                 and not re.search(r"\b" + re.escape(word) + r"\b", excerpt_lower)]
    if meta_hits:
        reasons.append(
            "meta_reference: the item mentions {0}, which the cited source lines "
            "do not. Training content describes the procedure, not the system "
            "that generated it.".format(", ".join(sorted(set(meta_hits)))))

    # De-duplicate while keeping order, so a sentence that trips two patterns of
    # the same kind reads as one reason.
    seen: set = set()
    unique: List[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            unique.append(reason)
    return unique


# ---------------------------------------------------------------------------
# Task A - objectives
# ---------------------------------------------------------------------------
def _enhance_objectives(module, sop, provider, config: LLMConfig,
                        report: EnhancementReport) -> None:
    originals = list(getattr(module, "learning_objectives", None) or [])
    if not originals:
        report.note("No deterministic objectives to improve.")
        return

    data = _call(provider, report, TASK_OBJECTIVES, sop,
                 objectives_prompt(originals), OBJECTIVES_SCHEMA)
    if data is None:
        return

    seen: set = set()
    for proposal in _as_list(data, "objectives"):
        index = proposal.get("index")
        text = str(proposal.get("objective") or "").strip()
        span = proposal.get("span")
        target = "objective[{0}]".format(index)

        if not isinstance(index, int) or isinstance(index, bool) \
                or not 0 <= index < len(originals):
            report.record(TASK_OBJECTIVES, target, False,
                          "Index {0!r} is not one of the {1} deterministic "
                          "objectives.".format(index, len(originals)),
                          proposed=text, span=span)
            continue
        if index in seen:
            report.record(TASK_OBJECTIVES, target, False,
                          "A replacement for this objective was already "
                          "proposed; the first one stands.",
                          original=originals[index], proposed=text, span=span)
            continue
        seen.add(index)

        if len(text) > MAX_OBJECTIVE_CHARS:
            report.record(TASK_OBJECTIVES, target, False,
                          "The objective is {0} characters; the limit is "
                          "{1}.".format(len(text), MAX_OBJECTIVE_CHARS),
                          original=originals[index], proposed=text, span=span)
            continue

        # Output filters before grounding: an objective that repeats an injected
        # line is grounded by construction, so grounding is the wrong gate for it.
        filtered = output_filter_reasons(
            text, span_excerpt(sop, span, config.span_context_lines))
        if filtered:
            report.record_output_filter(
                TASK_OBJECTIVES, target, filtered,
                original=originals[index], proposed=text, span=span)
            continue

        verdict = verify_claim(text, sop, span, config)
        accepted = report.record_verdict(
            TASK_OBJECTIVES, target, verdict,
            original=originals[index], proposed=text, span=span,
            accepted_reason="Bloom's-aligned rewrite supported by the cited lines.")
        if accepted:
            module.learning_objectives[index] = text
            _mirror_structured_objective(module, index, text, span,
                                         proposal.get("bloom_level"))


def _mirror_structured_objective(module, index: int, text: str, span,
                                 bloom_level) -> None:
    """Best-effort update of the structured ``objectives`` list, if present.

    A concurrent stream is adding ``TrainingModule.objectives`` (dicts with a
    ``source_ref``).  We do not depend on it and we do not create it; when it
    is there and index-aligned we keep it consistent with the string list so
    the two do not drift.  Any surprise in its shape is simply left alone.
    """
    structured = getattr(module, "objectives", None)
    if not isinstance(structured, list) or index >= len(structured):
        return
    entry = structured[index]
    if not isinstance(entry, dict):
        return
    for key in ("text", "statement", "objective"):
        if key in entry:
            entry[key] = text
            break
    else:
        return
    if bloom_level:
        entry["bloom_level"] = bloom_level
    if isinstance(span, (list, tuple)) and len(span) == 2:
        source_ref = entry.setdefault("source_ref", {})
        if isinstance(source_ref, dict):
            source_ref["llm_span"] = [span[0], span[1]]


# ---------------------------------------------------------------------------
# Task B - section summaries
# ---------------------------------------------------------------------------
def _enhance_summaries(module, sop, provider, config: LLMConfig,
                       report: EnhancementReport) -> None:
    sections = getattr(module, "sections", None) or []
    if not sections:
        report.note("No sections to summarise.")
        return

    data = _call(provider, report, TASK_SUMMARIES, sop,
                 summaries_prompt(sections), SUMMARIES_SCHEMA)
    if data is None:
        return

    by_id = {str(section.get("id")): section for section in sections
             if isinstance(section, dict)}
    done: set = set()

    for proposal in _as_list(data, "summaries"):
        section_id = str(proposal.get("section_id") or "")
        sentences = proposal.get("sentences")
        target = "section[{0}]".format(section_id or "?")

        if section_id not in by_id:
            report.record(TASK_SUMMARIES, target, False,
                          "No section with id {0!r} in this module.".format(
                              section_id))
            continue
        if section_id in done:
            report.record(TASK_SUMMARIES, target, False,
                          "A summary for this section was already proposed.")
            continue
        done.add(section_id)

        if not isinstance(sentences, list) \
                or not MIN_SUMMARY_SENTENCES <= len(sentences) <= MAX_SUMMARY_SENTENCES:
            report.record(TASK_SUMMARIES, target, False,
                          "A section summary must be {0}-{1} sentences; got "
                          "{2}.".format(MIN_SUMMARY_SENTENCES,
                                        MAX_SUMMARY_SENTENCES,
                                        len(sentences) if isinstance(sentences, list)
                                        else "none"))
            continue

        # Every sentence is checked on its own, and ONE failure rejects the
        # whole summary.  A paragraph shown to a learner in a regulated course
        # is either fully supported or it is not shown; there is no "mostly".
        texts: List[str] = []
        spans: List[Any] = []
        failures: List[str] = []
        filter_failures: List[str] = []
        for position, sentence in enumerate(sentences):
            if not isinstance(sentence, dict):
                failures.append("Sentence {0} is malformed.".format(position + 1))
                continue
            text = str(sentence.get("text") or "").strip()
            span = sentence.get("span")
            # Output filters first, and one filtered sentence rejects the whole
            # summary, exactly as one unsupported sentence does: a paragraph a
            # learner reads in a regulated course is either wholly clean or it is
            # not shown.
            filtered = output_filter_reasons(
                text, span_excerpt(sop, span, config.span_context_lines))
            if filtered:
                filter_failures.append("Sentence {0} ({1!r}): {2}".format(
                    position + 1, text[:60], " ".join(filtered)))
                continue
            verdict = verify_claim(text, sop, span, config)
            if verdict.ok:
                texts.append(text)
                spans.append(list(span))
            else:
                failures.append("Sentence {0} ({1!r}): {2}".format(
                    position + 1, text[:60], " ".join(verdict.reasons)))

        joined = " ".join(texts)
        if filter_failures:
            report.record_output_filter(
                TASK_SUMMARIES, target,
                ["{0} of {1} sentences were filtered, so the whole summary is "
                 "discarded.".format(len(filter_failures), len(sentences))]
                + filter_failures,
                proposed=joined, span=spans[0] if spans else None)
            continue
        if failures:
            report.record(
                TASK_SUMMARIES, target, False,
                "Rejected: {0} of {1} sentences are not supported by their "
                "citations, so the whole summary is discarded. {2}".format(
                    len(failures), len(sentences), " | ".join(failures)),
                proposed=joined,
                detail={"spans": spans})
            continue

        # Document text is untrusted and so is model text: escape before it
        # enters HTML (CLAUDE.md invariant).
        escaped = " ".join(html.escape(text) for text in texts)
        paragraph = '<p class="summary">{0}</p>'.format(escaped)
        section = by_id[section_id]
        section["content"] = paragraph + "\n" + (section.get("content") or "")
        report.record(TASK_SUMMARIES, target, True,
                      "All {0} sentences supported by their citations.".format(
                          len(texts)),
                      proposed=joined, span=spans[0] if spans else None,
                      detail={"spans": spans})


# ---------------------------------------------------------------------------
# Task C - distractors
# ---------------------------------------------------------------------------
def _mc_questions(assessment) -> List:
    return [q for q in getattr(assessment, "questions", [])
            if getattr(q, "type", "") == "multiple_choice"
            and len(getattr(q, "options", []) or []) >= MIN_OPTIONS]


def _question_digest_payload(questions) -> List[Dict[str, Any]]:
    payload = []
    for question in questions:
        correct = question.correct_option_text
        payload.append({
            "id": question.id,
            "text": question.text,
            "options": [(option, option == correct) for option in question.options],
        })
    return payload


def naive_pickers() -> Dict[str, Any]:
    """Every fixed strategy a learner can execute without reading the SOP.

    The position-dependent ones come straight from ``src.assessments``
    (``naive_strategies`` / ``strategy_pick``) so this check and M0's layout
    search agree by construction.  The three position-INDEPENDENT ones -
    "always the longest option", "always the shortest", "always True" -
    are built here because ``assessments`` deliberately does not model them in
    its layout search: they cannot be fixed by moving the answer, only by
    shaping the option text, which is exactly what swapping a distractor
    changes.  Leaving them out is how a "better" distractor could quietly
    re-open the defect M0 closed.
    """
    pickers: Dict[str, Any] = {}
    for strategy in naive_strategies(MAX_OPTIONS):
        kind, aim, fallback = strategy
        name = ("always_last_option" if kind == "last"
                else "always_option_{0}_else_{1}".format(aim + 1, fallback))
        pickers[name] = (lambda s: lambda q: strategy_pick(s, len(q.options)))(strategy)

    def longest(question):
        return max(range(len(question.options)),
                   key=lambda i: (len(question.options[i]), -i))

    def shortest(question):
        return min(range(len(question.options)),
                   key=lambda i: (len(question.options[i]), i))

    def always_true(question):
        options = question.options
        return options.index("True") if "True" in options else -1

    pickers["always_longest_option"] = longest
    pickers["always_shortest_option"] = shortest
    pickers["always_true"] = always_true
    return pickers


def worst_naive_score(assessment) -> Tuple[str, float]:
    """(strategy name, percentage) for the best a non-reader could do."""
    questions = list(getattr(assessment, "questions", []))
    total = sum(q.points for q in questions)
    if not total:
        return ("none", 0.0)
    worst_name, worst_score = "none", 0.0
    for name, picker in sorted(naive_pickers().items()):
        earned = sum(q.points for q in questions if picker(q) == q.correct_answer)
        score = 100.0 * earned / total
        if score > worst_score:
            worst_name, worst_score = name, score
    return worst_name, worst_score


def _canonicalise_options(questions) -> None:
    """Move each correct option back to index 0.

    ``_assign_answer_positions`` is written for freshly materialised questions,
    where ``options[0]`` is the correct answer and the rest are distractors in
    candidate order.  Questions that have already been through it are shuffled,
    so they have to be put back into that canonical form before it can be
    re-run.  Only questions it actually touches are canonicalised - true/false
    items keep their natural "True, False" order, exactly as in generation.
    """
    for question in questions:
        if question.type == "true_false":
            continue
        options = list(question.options or [])
        if len(options) < MIN_OPTIONS:
            continue
        index = question.correct_answer
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not 0 <= index < len(options):
            continue
        correct = options.pop(index)
        question.options = [correct] + options
        question.correct_answer = 0


def _enhance_distractors(assessment, sop, provider, config: LLMConfig,
                         report: EnhancementReport) -> bool:
    """Returns True when at least one distractor was replaced."""
    questions = _mc_questions(assessment)
    if not questions:
        report.note("No multiple-choice questions to improve.")
        return False

    data = _call(provider, report, TASK_DISTRACTORS, sop,
                 distractors_prompt(_question_digest_payload(questions)),
                 DISTRACTORS_SCHEMA)
    if data is None:
        return False

    by_id = {q.id: q for q in questions}
    changed = False
    handled: set = set()

    for proposal in _as_list(data, "questions"):
        question_id = str(proposal.get("question_id") or "")
        target = "question[{0}]".format(question_id or "?")
        question = by_id.get(question_id)
        if question is None:
            report.record(TASK_DISTRACTORS, target, False,
                          "No multiple-choice question with id {0!r} in this "
                          "assessment.".format(question_id))
            continue
        if question_id in handled:
            report.record(TASK_DISTRACTORS, target, False,
                          "Replacements for this question were already "
                          "proposed.")
            continue
        handled.add(question_id)

        correct_text = question.correct_option_text
        correct_norm = normalize_option_text(correct_text)
        working = list(question.options)
        # Normalised text -> index, so "replace" can be matched on the option's
        # meaning rather than on exact whitespace.
        index_by_norm = {normalize_option_text(option): position
                         for position, option in enumerate(working)}

        for replacement in proposal.get("replacements") or []:
            if not isinstance(replacement, dict):
                continue
            old = str(replacement.get("replace") or "").strip()
            new = str(replacement.get("with") or "").strip()
            span = replacement.get("span")
            why = str(replacement.get("why_false") or "").strip()
            slot = "{0}/{1}".format(target, old[:40] or "?")

            position = index_by_norm.get(normalize_option_text(old))
            if position is None:
                report.record(TASK_DISTRACTORS, slot, False,
                              "No option matching {0!r} on this question.".format(
                                  old[:80]),
                              proposed=new, span=span)
                continue
            if normalize_option_text(working[position]) == correct_norm:
                report.record(TASK_DISTRACTORS, slot, False,
                              "That is the correct option. The correct answer is "
                              "never changed.",
                              original=old, proposed=new, span=span)
                continue
            if resolve_span(sop, span) is None:
                report.record(TASK_DISTRACTORS, slot, False,
                              "The cited span {0!r} does not resolve to a line "
                              "range in the document; a distractor without a "
                              "traceable citation is not auditable.".format(span),
                              original=old, proposed=new, span=span)
                continue

            # A "wrong answer" carrying a URL, a contact or markup is a payload
            # dressed as an option, and it would be shown to every learner.
            filtered = output_filter_reasons(
                new, span_excerpt(sop, span, config.span_context_lines))
            if filtered:
                report.record_output_filter(
                    TASK_DISTRACTORS, slot, filtered,
                    original=old, proposed=new, span=span)
                continue

            verdict = verify_distractor(new, correct_text, sop, config)
            if verdict.ok:
                # Also guard against colliding with a sibling option.
                clash = [p for p, option in enumerate(working)
                         if p != position
                         and normalize_option_text(option) == normalize_option_text(new)]
                if clash:
                    report.record(TASK_DISTRACTORS, slot, False,
                                  "It duplicates another option on the same "
                                  "question.",
                                  original=old, proposed=new, span=span)
                    continue

            accepted = report.record_verdict(
                TASK_DISTRACTORS, slot, verdict,
                original=old, proposed=new, span=span,
                accepted_reason="False for this document; {0}".format(
                    why or "distinguishable from the correct answer."))
            if accepted:
                working[position] = new
                index_by_norm = {normalize_option_text(option): p
                                 for p, option in enumerate(working)}
                changed = True

        question.options = working

    return changed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def enhance_module(module, sop, provider=None,
                   config: Optional[LLMConfig] = None):
    """Return ``(TrainingModule, EnhancementReport)``.

    The input module is never mutated.  When the layer is off (or the provider
    is a ``NullProvider``) the returned module's ``to_dict()`` compares equal to
    the input's and the report is empty.
    """
    cfg = config or LLMConfig()
    report = EnhancementReport("module", cfg)
    if _is_off(provider, cfg):
        return module, report

    working = copy.deepcopy(module)
    try:
        _enhance_objectives(working, sop, provider, cfg, report)
        _enhance_summaries(working, sop, provider, cfg, report)
    except Exception as exc:                  # noqa: BLE001 - never crash a build
        report.note("Enhancement aborted after an unexpected {0}: {1}. The "
                    "deterministic module is used unchanged.".format(
                        type(exc).__name__, exc))
        return module, report

    if not report.accepted:
        return module, report
    return working, report


def enhance_assessment(assessment, sop, provider=None,
                       config: Optional[LLMConfig] = None):
    """Return ``(Assessment, EnhancementReport)``.

    The input assessment is never mutated.  If any distractor is replaced, the
    answer positions are re-assigned with M0's own machinery and every naive
    strategy is re-scored; a set of replacements that would let a non-reader
    reach the passing mark is reverted in full.
    """
    cfg = config or LLMConfig()
    report = EnhancementReport("assessment", cfg)
    if _is_off(provider, cfg):
        return assessment, report

    working = copy.deepcopy(assessment)
    try:
        changed = _enhance_distractors(working, sop, provider, cfg, report)
    except Exception as exc:                  # noqa: BLE001 - never crash a build
        report.note("Enhancement aborted after an unexpected {0}: {1}. The "
                    "deterministic assessment is used unchanged.".format(
                        type(exc).__name__, exc))
        return assessment, report

    if not changed:
        return assessment, report

    # Re-run M0's integrity machinery.  ``_assign_answer_positions`` is private
    # on purpose - it is not a stable public API - but it is imported here
    # deliberately rather than reimplemented: the naive-learner invariant is
    # defined BY that function's joint offset search, and a second
    # implementation of it here would be a second thing to keep in sync and the
    # first thing to drift. The alternative (regenerating the assessment) would
    # discard the enhanced options entirely.
    doc_key = document_key(getattr(sop, "title", "") or "",
                           getattr(sop, "version", "") or "")
    _canonicalise_options(working.questions)
    _assign_answer_positions(working.questions, doc_key)

    strategy, score = worst_naive_score(working)
    passing = float(getattr(working, "passing_score", 70) or 70)
    report.note(
        "Re-checked after {0} distractor replacement(s): the best a learner who "
        "never read the SOP can do is {1:.1f}% ({2}), against a passing score of "
        "{3:.0f}%.".format(len([i for i in report.accepted
                                if i.task == TASK_DISTRACTORS]),
                           score, strategy, passing))

    if score >= passing:
        for item in report.items:
            if item.task == TASK_DISTRACTORS and item.accepted:
                item.accepted = False
                item.reason = (
                    "Reverted: with this replacement applied, the fixed strategy "
                    "'{0}' would score {1:.1f}% against a passing score of "
                    "{2:.0f}%. The deterministic distractors are kept.".format(
                        strategy, score, passing))
        report.note(
            "ALL distractor changes reverted. Replacing distractors changes the "
            "options' length profile, and this set would have let '{0}' reach "
            "the passing mark - the defect M0 closed. The deterministic "
            "assessment is used unchanged.".format(strategy))
        return assessment, report

    return working, report
