"""Prompts and JSON schemas for the three enhancement tasks.

Two things here are load-bearing and must not be casually edited.

**The system prompt is byte-stable.**  No timestamps, no document title, no
question count - nothing that varies between runs.  It is sent as the first
``system`` block with ``cache_control``, the line-numbered document is sent as
the second, and the three calls for one document therefore share a cache
prefix: call one writes it, calls two and three read it at a tenth of the
price.  Interpolating anything per-run into either block silently throws that
away (``usage.cache_read_input_tokens`` going to zero is the tell).

**The document is presented with explicit line numbers.**  ``L0042: text``.
The model cites ``[start, end]`` spans of the numbers it was shown, and
:mod:`src.llm.grounding` then re-derives the excerpt from the same numbering
and checks the claim against it.  Without the numbering in the prompt the
citation would be a guess, and the whole grounding story collapses.
"""

from typing import Any, Dict, List, Sequence

from .grounding import document_lines

#: Width of the line-number gutter.  Fixed, so the prefix stays byte-stable
#: across documents of different lengths.
LINE_NUMBER_WIDTH = 4


STABLE_SYSTEM_PROMPT = """\
You are helping a regulated manufacturer (medical device / pharmaceutical) turn \
a controlled Standard Operating Procedure into training content. A quality \
auditor may read everything you write, alongside the source document, and a \
named subject-matter expert must approve it before any learner sees it. You are \
accelerating that expert's work. You are not replacing their judgment.

THE DOCUMENT
The SOP is supplied in the next system block with explicit 1-indexed line \
numbers in the form `L0042: text`. That numbering is your only citation \
vocabulary.

ABSOLUTE RULES
1. State only what the document states. If the document does not say it, you do \
   not write it - not as background, not as good practice, not as an obvious \
   inference. "The operator should wear gloves" is a violation unless the \
   document says so on a line you cite.
2. Never introduce a number, quantity, duration, tolerance, limit, threshold, \
   form number, channel, revision, department, role, name or step that is not \
   present in the cited lines. Numbers are checked mechanically against your \
   citation and a single invented figure rejects the whole item.
3. Prefer the document's own wording. Reuse its nouns, its verbs and its \
   terminology exactly; do not substitute a synonym for a defined term, and do \
   not soften or strengthen a requirement ("must" is not "should", "never" is \
   not "avoid").
4. Cite precisely. Every sentence you generate carries a span `[start, end]` of \
   line numbers you were actually shown, and those lines must contain the \
   support for that sentence. Cite the narrowest span that does. Never cite a \
   line number outside the document.
5. Say less rather than guess. Omitting an item is always acceptable; an \
   unsupported item is not. An automated grounding check rejects unsupported \
   sentences, and a rejected item simply falls back to the deterministic text.
6. Write plainly, for an operator on the shop floor, in the document's own \
   register. No marketing tone, no encouragement, no emoji, no HTML, no \
   markdown - plain sentences only.

WRONG ANSWERS (DISTRACTORS)
When you are asked for distractors, a distractor must be false *with respect to \
this document* and must not be true in general for the described procedure. It \
must be something a learner who has not read the SOP would find plausible and a \
learner who has read it would immediately reject. Specifically:
- Never offer a statement the document actually makes, anywhere, in any \
  section. That is not a wrong answer; it makes the question unanswerable, and \
  it is rejected automatically.
- Never offer generally-unsafe, absurd or joke options ("skip this step if you \
  are busy"). They give the answer away.
- Build the distractor from the document's own material: the wrong step of this \
  procedure, the wrong role, the wrong form, the wrong sequence, or a real \
  requirement with its polarity, threshold or actor changed.
- Keep it in the same register and roughly the same length as the correct \
  answer. Length is a tell, and a learner who spots it has learned nothing.
- Never change, comment on, or restate the correct answer.

OUTPUT
Return JSON matching the supplied schema and nothing else. Every field is \
required. If you cannot support an item honestly, leave it out of the array.\
"""


def format_document(sop) -> str:
    """The SOP with a 1-indexed line-number gutter: ``L0042: text``."""
    lines = document_lines(sop)
    return "\n".join(
        "L{0:0{1}d}: {2}".format(index, LINE_NUMBER_WIDTH, line)
        for index, line in enumerate(lines, 1)
    )


def system_blocks(sop) -> List[Dict[str, Any]]:
    """The cached prefix shared by all three calls for one document."""
    return [
        {"type": "text", "text": STABLE_SYSTEM_PROMPT,
         "cache_control": {"type": "ephemeral"}},
        {"type": "text",
         "text": "SOURCE DOCUMENT (1-indexed lines)\n\n" + format_document(sop),
         "cache_control": {"type": "ephemeral"}},
    ]


# ---------------------------------------------------------------------------
# Task A - Bloom's-aligned learning objectives
# ---------------------------------------------------------------------------
BLOOM_LEVELS = ("remember", "understand", "apply", "analyze", "evaluate", "create")

OBJECTIVES_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["objectives"],
    "properties": {
        "objectives": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "objective", "bloom_level", "span"],
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "0-based index of the deterministic "
                                       "objective this replaces.",
                    },
                    "objective": {
                        "type": "string",
                        "description": "One sentence, starting with a Bloom's "
                                       "action verb, observable and assessable.",
                    },
                    "bloom_level": {"type": "string", "enum": list(BLOOM_LEVELS)},
                    "span": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "[start, end] 1-indexed source lines.",
                    },
                },
            },
        },
    },
}


def objectives_prompt(objectives: Sequence[str]) -> str:
    listing = "\n".join(
        "  [{0}] {1}".format(index, text)
        for index, text in enumerate(objectives)
    ) or "  (none)"
    return """\
TASK: rewrite the learning objectives.

Below are the objectives the deterministic generator produced by slicing the \
document. They are accurate but not written as objectives: they name the step \
rather than the competence, and they are not aligned to Bloom's taxonomy.

{listing}

For each one you can genuinely improve, return a replacement that:
- opens with a Bloom's action verb appropriate to the competence actually \
  required (an operator executing a step is `apply`; recognising when the \
  procedure is in scope is `understand`; judging whether conditions for \
  restart are met is `evaluate`);
- is observable and assessable - what the learner will be able to DO, not what \
  they will "know" or "be aware of";
- **reuses the document's own wording.** Build the objective out of the nouns \
  and verbs on the lines you cite. Do not substitute synonyms for the \
  document's terms, and do not reach for smoother phrasing that drops them;
- **adds no action, condition or actor the cited lines do not contain.** Not a \
  follow-up step, not a precondition, not another role, not a "and then notify \
  X" - however obvious or sensible it seems. A faithful sentence with one \
  invented clause appended is the single worst thing you can return here, and \
  it is checked for explicitly;
- is one sentence, under 200 characters.

Keep the `index` of the objective you are replacing. Leave out any objective \
you cannot improve or cannot cite. Do not add objectives; do not renumber.\
""".format(listing=listing)


# ---------------------------------------------------------------------------
# Task B - plain-language section summaries
# ---------------------------------------------------------------------------
SUMMARIES_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summaries"],
    "properties": {
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["section_id", "sentences"],
                "properties": {
                    "section_id": {"type": "string"},
                    "sentences": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text", "span"],
                            "properties": {
                                "text": {"type": "string"},
                                "span": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


def summaries_prompt(sections: Sequence[Dict[str, Any]]) -> str:
    listing = "\n".join(
        "  {0}: {1}".format(section.get("id", "?"), section.get("title", ""))
        for section in sections
    ) or "  (none)"
    return """\
TASK: write a short plain-language summary for each training section.

The sections are:

{listing}

For each section you can summarise honestly from the document, return 2 to 4 \
sentences that tell an operator, before they read the detail, what this part of \
the procedure is about and what it requires of them.

Each sentence is cited separately with its own `[start, end]` span, and each is \
checked against those lines on its own: one unsupported sentence rejects the \
whole summary for that section, so keep every sentence tight to the text you \
cite.

Reuse the document's own wording - build each sentence from the nouns and verbs \
on the lines you cite, rather than restating them in smoother language. Add no \
action, condition or actor the cited lines do not contain: no extra step, no \
precondition the document does not state, no role it does not name. A sentence \
that quotes the procedure faithfully and then appends one invented clause is \
the worst possible output here, and it is checked for explicitly.

Plain sentences only - no HTML, no markdown, no lists. Under 300 characters per \
sentence.

Skip any section you cannot summarise from the document (an introduction whose \
content is only a title block, for instance). Use the section ids exactly as \
given.\
""".format(listing=listing)


# ---------------------------------------------------------------------------
# Task C - stronger distractors
# ---------------------------------------------------------------------------
DISTRACTORS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question_id", "replacements"],
                "properties": {
                    "question_id": {"type": "string"},
                    "replacements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["replace", "with", "why_false", "span"],
                            "properties": {
                                "replace": {
                                    "type": "string",
                                    "description": "The existing option text to "
                                                   "replace, copied exactly.",
                                },
                                "with": {
                                    "type": "string",
                                    "description": "The replacement wrong answer.",
                                },
                                "why_false": {
                                    "type": "string",
                                    "description": "One sentence: why this is "
                                                   "false for THIS document.",
                                },
                                "span": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "minItems": 2,
                                    "maxItems": 2,
                                    "description": "[start, end] lines the "
                                                   "distractor contradicts.",
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


def distractors_prompt(questions: Sequence[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for item in questions:
        options = "\n".join(
            "    - {0}{1}".format(text, "   <-- CORRECT, DO NOT TOUCH"
                                  if is_correct else "")
            for text, is_correct in item["options"]
        )
        blocks.append("  id: {0}\n  question: {1}\n  options:\n{2}".format(
            item["id"], item["text"], options))
    listing = "\n\n".join(blocks) or "  (none)"
    return """\
TASK: strengthen weak distractors.

Below are multiple-choice questions drawn from this document. The correct \
option is marked. The other options are wrong answers the deterministic \
generator drew from elsewhere in the document; some are weak - obviously \
off-topic, obviously the wrong shape, or not plausible enough to make the \
learner think.

{listing}

For each weak distractor you can do better than, return a replacement. Copy the \
option you are replacing into `replace` exactly as it appears above. Give the \
new wrong answer in `with`, one sentence in `why_false` saying what this \
document says that makes it false, and in `span` the lines it contradicts.

Rules, restated because they are the ones that get broken:
- NEVER propose replacing the correct option, and never alter it.
- The replacement must be FALSE for this document. If the document asserts it \
  anywhere - in another section, another step, another warning - it is not a \
  distractor and it will be rejected automatically.
- It must also not be true in general for the described procedure. A learner \
  should reject it because this SOP says otherwise, not because it is silly.
- Match the correct option's length and register closely.
- Leave out any question you cannot improve. A weak distractor kept is better \
  than a broken one introduced.\
""".format(listing=listing)
