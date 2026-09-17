"""Pre-flight scan of an uploaded document for prompt-injection material.

The product ingests documents it did not write.  When the optional LLM layer
(:mod:`src.llm`) is enabled, the text of those documents is sent to a model; and
whether or not it is, that text ends up in learner HTML, an SME review page, a
transparency report, an audit-detail JSON blob and CLI output.  An attacker who
controls the document therefore has four things to try, and ``docs/SECURITY.md``
states them in full:

a. make the model emit attacker text into training content;
b. make the model weaken the assessment (a wrong option marked correct);
c. make the model disclose its own instructions or other context;
d. break a *non*-LLM surface - HTML into a page, an escape sequence into a
   terminal, a control character into JSON.

This module is one layer of the answer and, deliberately, not the whole answer.
It is a **lexical, deterministic, pure-stdlib** scan: it finds the phrasings and
character tricks an injection attempt normally uses, reports them with line
numbers so a human can look, and lets the caller gate the LLM layer on the
result.  It cannot recognise an instruction phrased in ordinary procedural
English, and it is not a substitute for the layers around it (prompt design,
output filters, escaping everywhere, and SME approval).

Two things about *grounding*, because it is the defence people assume covers
this and it does not: :mod:`src.llm.grounding` checks that a generated sentence
is supported by the document lines the model cited.  Injected text **is** in the
document, so a sentence that faithfully repeats it is, by construction,
grounded.  Grounding defends against invention, not against the document lying
to the model.  That is why this scanner and the output filters in
:mod:`src.llm.enhance` exist.

Risk levels
-----------
``"high"``
    An instruction-to-AI phrasing, a role/prompt delimiter, or hidden or
    obfuscated text (zero-width, invisible/tag characters, bidi override,
    homoglyphs, base64 blob, ANSI escape, stray control characters).  Callers gate
    the LLM layer on this.  Phrasing families are verb x object matched across a
    clause-bounded window, so a synonym does not walk through, and an override
    phrase preceded by a negation is *not* a finding - "never ignore an audible
    alarm" is a procedure, "ignore all previous instructions" is not.
``"low"``
    URLs, e-mail addresses, renderable markup (``<script``, ``javascript:``,
    ``onclick=``), or a line repeated to excess.  Worth a reviewer's eye; not
    worth refusing to run on.  Markup is *low* on purpose: every document-derived
    string is HTML-escaped before it enters a page (a CLAUDE.md invariant with
    its own tests), so markup in the source is inert - it is a signal about the
    document's provenance, not a live hazard.
``"none"``
    Nothing matched.  All eight documents shipped in this repository
    (``examples/sample_sop*.txt`` and the six ``examples/gallery`` SOPs) must
    stay at ``"none"``, and so must a corpus of real web-sourced procedures
    (``tests/test_injection.py``, ``INJECTION_SCAN_CORPUS_DIR``; last measured:
    107 documents, 101 none / 6 low / 0 high) - because a scanner that cries wolf
    on a real SOP would just get switched off.  Widening a pattern means
    re-measuring against that corpus.

The deterministic pipeline runs regardless of the verdict.  It is regex and
string slicing: it cannot be talked into anything, its output is escaped
everywhere it is rendered, and refusing to generate training from a flagged
document would only hand an attacker a denial-of-service.  The *LLM* layer is
what gets gated, because a model is the only component here that can be
persuaded.
"""

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

RISK_NONE = "none"
RISK_LOW = "low"
RISK_HIGH = "high"

#: Order matters for reporting only: worst first.
RISK_ORDER = (RISK_HIGH, RISK_LOW, RISK_NONE)

# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------
#: Kinds that mean "a human wrote text at the model, inside a controlled
#: document".  Any one of these makes the document ``high`` risk.
KIND_INSTRUCTION_OVERRIDE = "instruction_override"
KIND_ROLE_ASSIGNMENT = "role_assignment"
KIND_AI_ADDRESSED = "ai_addressed"
KIND_PROMPT_DISCLOSURE = "prompt_disclosure"
KIND_ROLE_MARKER = "role_marker"
KIND_PROMPT_DELIMITER = "prompt_delimiter"
KIND_GENERATION_DIRECTIVE = "generation_directive"

#: Kinds that mean "there is text here a reader cannot see, or cannot see
#: correctly".  Also ``high``: hidden text has no legitimate use in a
#: controlled procedure, and it is how the phrasings above get past a reviewer.
KIND_ZERO_WIDTH = "zero_width"
KIND_INVISIBLE_CHARS = "invisible_chars"
KIND_BIDI_OVERRIDE = "bidi_override"
KIND_HOMOGLYPH = "homoglyph"
KIND_BASE64_BLOB = "base64_blob"
KIND_ANSI_ESCAPE = "ansi_escape"
KIND_CONTROL_CHAR = "control_char"

#: Kinds worth showing a reviewer that do not, on their own, justify refusing
#: to run the LLM layer.
KIND_URL = "url"
KIND_EMAIL = "email"
KIND_HTML_MARKUP = "html_markup"
KIND_JS_URI = "js_uri"
KIND_EVENT_HANDLER = "event_handler"
KIND_REPETITION = "repetition"

HIGH_KINDS = frozenset({
    KIND_INVISIBLE_CHARS,
    KIND_INSTRUCTION_OVERRIDE,
    KIND_ROLE_ASSIGNMENT,
    KIND_AI_ADDRESSED,
    KIND_PROMPT_DISCLOSURE,
    KIND_ROLE_MARKER,
    KIND_PROMPT_DELIMITER,
    KIND_GENERATION_DIRECTIVE,
    KIND_ZERO_WIDTH,
    KIND_BIDI_OVERRIDE,
    KIND_HOMOGLYPH,
    KIND_BASE64_BLOB,
    KIND_ANSI_ESCAPE,
    KIND_CONTROL_CHAR,
})

LOW_KINDS = frozenset({
    KIND_URL,
    KIND_EMAIL,
    KIND_HTML_MARKUP,
    KIND_JS_URI,
    KIND_EVENT_HANDLER,
    KIND_REPETITION,
})

#: One line of plain English per kind, for the review banner and the report.
KIND_DESCRIPTIONS: Dict[str, str] = {
    KIND_INSTRUCTION_OVERRIDE:
        "Text telling a reader (or a model) to ignore or override instructions.",
    KIND_ROLE_ASSIGNMENT:
        "Text assigning a new role or persona, in the second person.",
    KIND_AI_ADDRESSED:
        "Text addressed to an AI assistant or language model.",
    KIND_PROMPT_DISCLOSURE:
        "Text asking for a system prompt, hidden instructions or concealment.",
    KIND_ROLE_MARKER:
        "A chat role marker (`System:`, `User:`, `Assistant:`) at the start of a line.",
    KIND_PROMPT_DELIMITER:
        "A prompt-format delimiter or role fence (`<|...|>`, `[INST]`, "
        "`### Instruction`, `BEGIN SYSTEM`).",
    KIND_GENERATION_DIRECTIVE:
        "An instruction about how this document should be summarised, or how "
        "training or quiz content drawn from it should come out.",
    KIND_ZERO_WIDTH:
        "Zero-width characters: text a reviewer cannot see on the page.",
    KIND_INVISIBLE_CHARS:
        "Invisible or formatting characters (Unicode tag characters, soft "
        "hyphens, invisible operators, line/paragraph separators): text that "
        "carries content a reviewer cannot see.",
    KIND_BIDI_OVERRIDE:
        "Bidirectional override characters: text that displays in an order "
        "other than the one it is stored in.",
    KIND_HOMOGLYPH:
        "Look-alike characters from another script mixed into Latin words.",
    KIND_BASE64_BLOB:
        "A long encoded-looking run with no spaces: content a reviewer cannot read.",
    KIND_ANSI_ESCAPE:
        "ANSI terminal escape sequences, which can rewrite or hide terminal output.",
    KIND_CONTROL_CHAR:
        "Control characters other than tab and newline.",
    KIND_URL:
        "A URL. Procedure text rarely needs one, and it is how an instruction "
        "reaches a learner's browser.",
    KIND_EMAIL:
        "An e-mail address.",
    KIND_HTML_MARKUP:
        "HTML that would render if it were not escaped.",
    KIND_JS_URI:
        "A `javascript:` URI.",
    KIND_EVENT_HANDLER:
        "An inline HTML event handler (`onclick=`, `onerror=`, ...).",
    KIND_REPETITION:
        "The same line repeated many times, which is how a reviewer's eye is "
        "worn down and how context is flooded.",
}


# ---------------------------------------------------------------------------
# Patterns
#
# Two forces shape every pattern here, and they pull in opposite directions.
#
# *Recall*: an attacker writes prose, not tokens, so a pattern that only matches
# one phrasing catches one attacker.  The verb/object families below are matched
# across a window rather than adjacently, so "disregard all earlier guidance"
# lands the same way "ignore previous instructions" does.
#
# *Precision*: zero findings on real procedures is a hard requirement
# (tests/test_injection.py runs the eight documents this repository ships, a
# curated list of real procedural sentences, and - when
# INJECTION_SCAN_CORPUS_DIR is set - a corpus of real web-sourced procedures).
# A scanner that fires on a real SOP gets switched off, and then it defends
# nothing.
#
# The discriminator that makes both possible is almost always *what the sentence
# is about*.  A procedure talks about the document's own instructions ("do not
# ignore the manufacturer's instructions"); an injection talks about the
# reader's standing instructions ("ignore all previous instructions").  So the
# override family needs a precedence or possessive qualifier - all, any, earlier,
# prior, previous, preceding, above, original, your - and not merely an
# instruction-shaped noun.  Same idea throughout: "state that" must open a
# clause (not "studies state that"), "AI" must be an addressee (not "the
# AI-assisted inspection camera"), "###" must be followed by a role word (half
# the gallery is markdown).
# ---------------------------------------------------------------------------

#: Verbs an override instruction is built from.  ``set aside`` and ``pay no
#: attention to`` are multi-word, hence the alternation rather than a word list.
_OVERRIDE_VERBS = (
    r"(?:ignor\w*|disregard\w*|forget|override|overrid\w+|bypass\w*|discard\w*|"
    r"skip|set\s+aside|pay\s+no\s+attention\s+to|do\s+not\s+follow|"
    r"don'?t\s+follow|no\s+longer\s+follow|never\s+follow|stop\s+following)"
)

#: Qualifiers that mark the object as *the reader's standing instructions*
#: rather than the document's own.  Required, and this is the whole reason the
#: family does not fire on "do not ignore the manufacturer's instructions".
_OVERRIDE_QUALIFIERS = (
    r"(?:all|any|every|earlier|prior|previous|preceding|foregoing|above|"
    r"original|initial|former|old|existing|your)"
)

#: Nouns that name instructions.  Deliberately wide, because the qualifier above
#: is carrying the precision.
_OVERRIDE_OBJECTS = (
    r"(?:instruction|instructions|guidance|guidelines?|rules?|prompts?|"
    r"directions?|directives?|context|constraints?|restrictions?|guardrails?|"
    r"system\s+messages?|training|programming|orders?)"
)

#: Up to six words may sit between the verb and its object, so the window is
#: generous but still bounded to one clause (no sentence-ending punctuation).
_WINDOW = r"(?:[^.;:!?\n]{0,60}?)"

_TEXT_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    # -- instruction-to-AI phrasings ---------------------------------------
    # verb -> (qualifier) -> object, across a clause-bounded window.
    (KIND_INSTRUCTION_OVERRIDE, re.compile(
        r"\b" + _OVERRIDE_VERBS + r"\b" + _WINDOW
        + r"\b" + _OVERRIDE_QUALIFIERS + r"\s+(?:\w+\s+){0,2}?"
        + _OVERRIDE_OBJECTS + r"\b",
        re.IGNORECASE)),
    # "…the above", "…everything above/before", "…what you were told".
    (KIND_INSTRUCTION_OVERRIDE, re.compile(
        r"\b" + _OVERRIDE_VERBS + r"\b" + _WINDOW
        + r"\b(?:(?:the|everything|anything|all)\s+"
          r"(?:above|before|preceding|foregoing|earlier|prior)\b|"
          r"what\s+you\s+(?:were|have\s+been)\s+told\b|"
          r"what\s+(?:came|went)\s+before\b)",
        re.IGNORECASE)),
    # A fresh instruction block announced as such.
    (KIND_INSTRUCTION_OVERRIDE, re.compile(
        r"\bnew\s+(?:instructions?|directive|task|rules?|prompt)\s*[:\-]",
        re.IGNORECASE)),
    (KIND_INSTRUCTION_OVERRIDE, re.compile(
        r"\b(?:instead\s+of|rather\s+than)\s+(?:following|obeying)\s+"
        r"(?:the|your|those|these|all)\b",
        re.IGNORECASE)),
    # -- role assignment ---------------------------------------------------
    # "from now on you are …" / "you are now …" / "you are no longer …":
    # the cue is the *re-assignment*, which is why plain "if you are a nurse"
    # (very common in real procedures) is not matched.
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\b(?:from\s+now\s+on|starting\s+now|for\s+the\s+rest\s+of\s+this)\b"
        r"[^.\n]{0,20}?\byou\s+(?:are|will\s+be|must\s+be|act)\b",
        re.IGNORECASE)),
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\byou\s+are\s+(?:now|no\s+longer|hereby|henceforth)\b", re.IGNORECASE)),
    # "you are DAN", "you are Sydney, a chat mode": a *persona* as the
    # predicate.  Two narrow shapes only - an ALL-CAPS name (the jailbreak
    # personas are all shaped like this) or a capitalised name introduced by an
    # apposition - because "if you are British, use UK spelling" is an ordinary
    # sentence and must not fire.
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\b[Yy]ou\s+are\s+(?:an?\s+|the\s+)?(?:[A-Z]{2,}[A-Za-z0-9_-]*)\b")),
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\b[Yy]ou\s+are\s+(?:an?\s+|the\s+)?[A-Z][A-Za-z0-9_-]+\s*,\s*"
        r"(?:a|an|the)\b")),
    # "you are a model/assistant/AI …", with or without a jailbreak tail.
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\byou\s+are\s+(?:an?|the)\s+(?:\w+\s+){0,2}?"
        r"(?:ai|a\.i\.|llm|language\s+model|model|assistant|chatbot|bot|agent|"
        r"system)\b",
        re.IGNORECASE)),
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\bwith(?:out)?\s+(?:no\s+|any\s+)?"
        r"(?:restrictions?|limits?|limitations?|filters?|guardrails?|rules?)\b"
        r"[^.\n]{0,20}?\b(?:you|model|assistant|ai)\b|"
        r"\byou\s+(?:are|have)\b[^.\n]{0,30}?\b(?:no|without)\s+"
        r"(?:restrictions?|limits?|limitations?|filters?|guardrails?)\b",
        re.IGNORECASE)),
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\b(?:act|behave|respond|answer|speak|reply)\s+as\s+(?:an?\s+|if\s+you\s+)",
        re.IGNORECASE)),
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\bpretend(?:\s+to\s+be|\s+that\s+you)?\b|\brole[\s-]?play\b|"
        r"\byour\s+new\s+(?:role|task|job|persona|identity|instructions?)\b|"
        r"\bassume\s+the\s+(?:role|persona|identity)\s+of\b",
        re.IGNORECASE)),
    # "you will now <verb>" only for verbs that reassign behaviour - "you will
    # now see the settings menu" is a real procedure sentence.
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\byou\s+will\s+now\s+(?:act|be|behave|respond|answer|reply|obey|"
        r"follow|ignore|pretend|only|always|never|output|write|say|generate|"
        r"summarise|summarize|operate|function)\b",
        re.IGNORECASE)),
    # -- addressed to a model ----------------------------------------------
    # "Note to the AI assistant …", "Message for the model …": the addressee
    # construction is required, so "the AI-assisted camera" is not a finding.
    (KIND_AI_ADDRESSED, re.compile(
        r"\b(?:note|notes|message|instruction|instructions|reminder|directive|"
        r"memo|aside|comment|hint|tip|attention|prompt)\s+"
        r"(?:to|for)\s+(?:the\s+|any\s+|all\s+)?"
        r"(?:ai|a\.i\.|artificial\s+intelligence|assistant|ai\s+assistant|"
        r"model|language\s+model|llm|chatbot|bot|machine|system|"
        r"summaris\w+|summariz\w+|reviewer\s+bot|automated\s+\w+|"
        r"generator|claude|chatgpt|gpt|copilot|gemini)\b",
        re.IGNORECASE)),
    # A vocative: "AI assistant, …", "Dear language model", "Hey Claude:".
    (KIND_AI_ADDRESSED, re.compile(
        r"\b(?:dear|hey|hello|hi|ok|okay|attention)\s+"
        r"(?:ai|a\.i\.|assistant|ai\s+assistant|language\s+model|model|llm|"
        r"chatbot|claude|chatgpt|gpt|copilot|gemini)\b",
        re.IGNORECASE)),
    (KIND_AI_ADDRESSED, re.compile(
        r"\b(?:ai|a\.i\.|assistant|ai\s+assistant|language\s+model|llm|chatbot|"
        r"claude|chatgpt|gpt|copilot|gemini)\s*[,:]\s*"
        r"(?:please\s+|kindly\s+|now\s+)?"
        r"(?:ignore|disregard|note|include|add|remember|output|write|say|do|"
        r"mark|state|make|set|use|read|follow|summaris\w+|summariz\w+)\b",
        re.IGNORECASE)),
    # "as an AI", "you are an AI language model" - the self-reference an
    # injection uses to explain to the model what it supposedly is.
    (KIND_AI_ADDRESSED, re.compile(
        r"\bas\s+an?\s+(?:ai|a\.i\.|llm|language\s+model|chatbot|"
        r"artificial\s+intelligence)\b",
        re.IGNORECASE)),
    # An addressee in the second person: "AI assistant summarising this", "the
    # model reading this document".
    (KIND_AI_ADDRESSED, re.compile(
        r"\b(?:ai\s+assistant|language\s+model|llm|chatbot|ai\s+model|"
        r"ai\s+system|assistant)\s+"
        r"(?:that\s+|which\s+|who\s+)?"
        r"(?:is\s+|are\s+)?"
        r"(?:summaris\w+|summariz\w+|generat\w+|read\w*|process\w*|writ\w+|"
        r"creat\w+|review\w*|train\w*)\b",
        re.IGNORECASE)),
    # -- asking for the prompt, or for concealment -------------------------
    (KIND_PROMPT_DISCLOSURE, re.compile(
        r"\b(?:system|initial|hidden|original)\s+prompt\b", re.IGNORECASE)),
    (KIND_PROMPT_DISCLOSURE, re.compile(
        r"\b(?:reveal|disclose|print|output|repeat|show|expose|leak|dump|"
        r"echo|recite)\b"
        r"[^.\n]{0,30}?\b(?:your|the|these|its)\s+"
        r"(?:system\s+|hidden\s+|initial\s+|original\s+|full\s+)?"
        r"(?:prompt|prompts|instructions|guidelines|configuration|context|"
        r"rules)\b",
        re.IGNORECASE)),
    (KIND_PROMPT_DISCLOSURE, re.compile(
        r"\b(?:do\s*n[o']?t|never|don't|without)\s+"
        r"(?:tell|telling|mention|mentioning|reveal|revealing|disclose|"
        r"disclosing|show|showing|informing|inform)\b"
        r"[^.\n]{0,30}?\b(?:the\s+)?(?:user|reviewer|human|reader|anyone|sme|"
        r"operator\s+reading)\b",
        re.IGNORECASE)),
    (KIND_PROMPT_DISCLOSURE, re.compile(
        r"\bkeep\s+th(?:is|ese)\s+"
        r"(?:instructions?|note|text|message|secret|part|section)\b"
        r"[^.\n]{0,20}?\b(?:secret|hidden|confidential|to\s+yourself|"
        r"between\s+us)\b",
        re.IGNORECASE)),
    # -- directives about the generated training ---------------------------
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\bwhen\s+(?:you\s+)?"
        r"(?:summaris\w+|summariz\w+|generat\w+|creat\w+|writ\w+|produc\w+|"
        r"paraphras\w+|translat\w+|process\w+|read\w+|convert\w*|train\w*)\b"
        r"[^.\n]{0,40}?"
        r"\b(?:this|the|these)\s+(?:document|sop|procedure|text|training|"
        r"content|instructions?|material)\b",
        re.IGNORECASE)),
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\b(?:when|while|if|before)\s+(?:you\s+(?:are\s+)?)?"
        r"(?:generating|creating|writing|producing|building|drafting|making|"
        r"preparing)\s+"
        r"(?:the\s+|any\s+|a\s+)?(?:training|course|quiz|question|questions|"
        r"assessment|summary|summaries|objectives?|module|lesson)\b",
        re.IGNORECASE)),
    # "option B is always correct", "answer 3 as correct", "mark option A".
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\b(?:option|answer|choice|response)\s*[\"'(\[]?[A-Da-d1-4][\"')\]]?\s*"
        r"(?:\w+\s+){0,3}?\b(?:is|as|are|be)\s+(?:always\s+|the\s+)?"
        r"(?:correct|right|true)\b",
        re.IGNORECASE)),
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\b(?:mark|make|treat|select|choose|pick|set)\s+"
        r"(?:option|answer|choice|response)\b",
        re.IGNORECASE)),
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\b(?:mark|make|treat|select|choose|pick)\s+(?:option\s+|answer\s+|"
        r"choice\s+)?[\"'(\[]?[a-dA-D1-4][\"')\]]?\b[^.\n]{0,25}?"
        r"\b(?:correct|right|the\s+answer)\b",
        re.IGNORECASE)),
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\b(?:correct\s+answer|answer\s+key)\b[^.\n]{0,25}?"
        r"\b(?:always|every\s+question|for\s+all\s+questions)\b",
        re.IGNORECASE)),
    # "state that …" as an imperative opening a clause; "studies state that"
    # (a noun subject) is not an instruction to anybody.
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"(?:^|[.;:!?]\s+|\band\s+|\bthen\s+|\bplease\s+|\balso\s+|,\s*)"
        r"(?:state|say|claim|assert|report|write|answer)\s+that\b",
        re.IGNORECASE)),
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\btell\s+(?:the\s+)?(?:learner|learners|reader|readers|user|users|"
        r"trainee|trainees|student|students|them)\b",
        re.IGNORECASE)),
    # "include the link in every summary", "add the phone number … to every
    # objective".  The *destination* has to be a training artifact - an
    # objective, a question, a summary, the course - because "add the link URL in
    # the second box" and "include the phone number of the recipient in the
    # letter header" are ordinary procedure steps, and an earlier draft of this
    # pattern flagged both (one of them inside the real-document corpus).  The
    # narrower recall is backed up by the output filters, which reject a URL or a
    # phone number in generated text however it got there.
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\b(?:include|add|insert|append|put|embed|mention)\b[^.\n]{0,40}?"
        r"\b(?:in|into|to)\s+"
        r"(?:the\s+|every\s+|each\s+|any\s+|all\s+|your\s+)?"
        r"(?:objective|objectives|question|questions|summary|summaries|"
        r"answer|answers|quiz|training|course|module|lesson)\b",
        re.IGNORECASE)),
    # -- prompt-format delimiters and role fences --------------------------
    (KIND_PROMPT_DELIMITER, re.compile(r"<\|[^\n]{0,40}?\|?>")),
    (KIND_PROMPT_DELIMITER, re.compile(r"\[/?INST\]|\[/?SYS\]|<</?SYS>>")),
    (KIND_PROMPT_DELIMITER, re.compile(r"<\|?(?:im_start|im_end|endoftext)\|?>")),
    # An XML-ish role tag on its own: <system>, </assistant>, <system_prompt>.
    (KIND_PROMPT_DELIMITER, re.compile(
        r"</?\s*(?:system|assistant|user|human|instructions?|prompt|"
        r"system_prompt|im_start|im_end)\s*/?>",
        re.IGNORECASE)),
    # A fenced code block whose info string is a role - ```system, ~~~assistant.
    (KIND_PROMPT_DELIMITER, re.compile(
        r"^\s*(?:`{3,}|~{3,})\s*"
        r"(?:system|assistant|user|human|instruction|instructions|prompt|"
        r"context|developer)\b\s*$",
        re.IGNORECASE)),
    # `###` is a markdown heading in half the SOPs in this repository, so it is
    # a fence only when what follows it is a *role*, Alpaca-style.
    (KIND_PROMPT_DELIMITER, re.compile(
        r"^\s*#{2,}\s*(?:instruction|instructions|system|response|human|"
        r"assistant|user|input|output|context|prompt)\b\s*:?\s*$",
        re.IGNORECASE)),
    (KIND_PROMPT_DELIMITER, re.compile(
        r"\b(?:BEGIN|END|START|STOP)[ _-]+"
        r"(?:SYSTEM|PROMPT|INSTRUCTION|INSTRUCTIONS|CONTEXT|ASSISTANT|USER)\b")),
    (KIND_ROLE_MARKER, re.compile(
        r"^\s*(?:system|assistant|user|human|ai)\s*:",
        re.IGNORECASE)),
    # -- renderable markup (low) -------------------------------------------
    (KIND_HTML_MARKUP, re.compile(
        r"</?\s*(?:script|iframe|object|embed|svg|img|style|form|input|link|meta|"
        r"body|html|base)\b",
        re.IGNORECASE)),
    (KIND_JS_URI, re.compile(r"\bjavascript\s*:", re.IGNORECASE)),
    (KIND_JS_URI, re.compile(r"\bdata\s*:\s*text/html", re.IGNORECASE)),
    (KIND_EVENT_HANDLER, re.compile(r"\bon[a-z]{3,20}\s*=\s*[\"'a-z0-9]",
                                    re.IGNORECASE)),
    # -- contact details (low) ---------------------------------------------
    (KIND_URL, re.compile(r"\b(?:https?|ftp|file)://\S+", re.IGNORECASE)),
    (KIND_URL, re.compile(r"\bwww\.[a-z0-9-]+\.[a-z]{2,}\b", re.IGNORECASE)),
    (KIND_EMAIL, re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
)

#: Invisible characters that carry no legitimate meaning in a procedure.
ZERO_WIDTH_CHARS = "​‌‍‎‏⁠﻿"
BIDI_CHARS = "‪‫‬‭‮⁦⁧⁨⁩"

_ZERO_WIDTH_RE = re.compile("[" + ZERO_WIDTH_CHARS + "]")
_BIDI_RE = re.compile("[" + BIDI_CHARS + "]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\x1b")
#: Everything in C0/C1 except tab, newline and carriage return.  ESC is
#: reported as ``ansi_escape`` instead, so it is excluded here.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f-\x9f]")
#: Word tokens, for the mixed-script (homoglyph) test.
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

#: A long unbroken run that looks encoded rather than written.
_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")

#: Invisible and formatting characters beyond the zero-width set above.  Unicode
#: TAG characters (U+E0000-U+E007F) are the important ones: they mirror ASCII,
#: render as nothing at all, and are a known carrier for a whole instruction
#: hidden behind an innocuous sentence.  The rest are the characters that show up
#: in the same role - soft hyphens used to break a keyword up, the invisible
#: math operators, CGJ, the Arabic letter mark, Mongolian vowel separator, the
#: line/paragraph separators, and the interlinear annotation marks.
_TAG_CHAR_RE = re.compile(r"[\U000E0000-\U000E007F]")
_INVISIBLE_SINGLE_CHARS = "\u034f\u061c\u180e\u2028\u2029\u2061\u2062\u2063\u2064\ufff9\ufffa\ufffb"
_INVISIBLE_SINGLE_RE = re.compile("[" + _INVISIBLE_SINGLE_CHARS + "]")
#: A soft hyphen is legitimate typography in isolation (hyphenation hints in
#: copied text), so only a *run* of them - the shape used to break a word up so a
#: keyword check misses it - is reported.
_SOFT_HYPHEN_RUN_RE = re.compile(r"(?:\u00ad[^\u00ad]{0,3}){2,}")
#: Variation selectors carry emoji presentation legitimately, so they only count
#: when the character they follow is a letter or digit - i.e. when they are
#: decorating text rather than a symbol.
_VARIATION_SELECTOR_RE = re.compile(r"[0-9A-Za-z][\ufe00-\ufe0f]")

#: Text that a base64 blob has to decode into before it is worth re-scanning:
#: mostly printable, and long enough to carry a sentence.
_BASE64_MIN_DECODED_CHARS = 12
_BASE64_MIN_PRINTABLE_RATIO = 0.9

#: A negation in front of an override phrase turns it from an instruction into a
#: *warning about* one, which is what real procedures contain: "never ignore an
#: audible alarm", "do not ignore any safety rules". The window is short on
#: purpose - it is the immediate verb phrase, not the whole sentence.
_NEGATED_OVERRIDE_RE = re.compile(
    r"(?:do\s*not|do\s+not\s+ever|don'?t|never|cannot|can'?t|must\s+not|"
    r"shall\s+not|should\s+not|avoid|failure\s+to|without)\s+(?:\w+\s+){0,2}$",
    re.IGNORECASE)
#: How far back the negation veto looks from the start of a match.
_NEGATION_LOOKBACK = 28

#: Minimum times a line must repeat, and its minimum length, before repetition
#: is worth reporting.  A short repeated line ("N/A", "---", a blank cell) is
#: ordinary document furniture; a long one repeated five times is not.
REPETITION_THRESHOLD = 5
REPETITION_MIN_CHARS = 20

#: Non-ASCII letters that read as ASCII ones.  Deliberately small: these are
#: the ones actually used to smuggle a word past a reviewer.
HOMOGLYPHS = (
    "АВЕЗКМНОРСТУ"
    "Хаеорсухіј"        # Cyrillic
    "ΑΒΕΖΗΙΚΜΝΟΡ"
    "ΤΥΧαειορυν"        # Greek
    "‐–—−"                                            # dashes
)
#: Dashes are common typography, so they do not count towards the homoglyph
#: score - only letters do.
_HOMOGLYPH_LETTERS = frozenset(
    ch for ch in HOMOGLYPHS if unicodedata.category(ch).startswith("L"))
#: How many look-alike letters must sit inside *mixed-script words* before a line
#: is called a homoglyph attack.  One is enough, because the test is mixed script
#: within a single word ("Pr<Cyrillic e>ss"), which no ordinary document does; a
#: document legitimately written in Cyrillic or Greek has whole words in one
#: script and is not flagged at all.
HOMOGLYPH_MIN_COUNT = 1

#: How much of a line is quoted in a finding.  Long enough to recognise, short
#: enough that the banner stays readable.
EXCERPT_CHARS = 160


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    """One thing the scan found, at one 1-indexed line.

    ``excerpt`` is the line rendered *visibly*: invisible and control
    characters are replaced by ``<U+200B>``-style markers, so a reviewer can
    see what is actually there and so the finding itself cannot carry the
    payload onward into a page, a log or a terminal.
    """

    line: int
    kind: str
    excerpt: str

    @property
    def severity(self) -> str:
        return RISK_HIGH if self.kind in HIGH_KINDS else RISK_LOW

    @property
    def description(self) -> str:
        return KIND_DESCRIPTIONS.get(self.kind, "")

    def to_dict(self) -> Dict:
        return {"line": self.line, "kind": self.kind, "excerpt": self.excerpt,
                "severity": self.severity, "description": self.description}


@dataclass(frozen=True)
class ScanResult:
    """The verdict for one document.

    ``risk`` is ``"high"`` / ``"low"`` / ``"none"``; ``findings`` is ordered by
    (line, kind) so two scans of the same bytes produce the same report.
    """

    risk: str = RISK_NONE
    findings: Tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def kinds(self) -> List[str]:
        """Distinct kinds found, high-severity ones first, then alphabetical."""
        seen = {finding.kind for finding in self.findings}
        return sorted(seen, key=lambda kind: (kind not in HIGH_KINDS, kind))

    @property
    def high_kinds(self) -> List[str]:
        return [kind for kind in self.kinds if kind in HIGH_KINDS]

    @property
    def clean(self) -> bool:
        return self.risk == RISK_NONE

    @property
    def blocks_llm(self) -> bool:
        """True when the LLM layer must not be run on this document."""
        return self.risk == RISK_HIGH

    def counts_by_kind(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for finding in self.findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        return counts

    def summary(self) -> str:
        if self.clean:
            return "No prompt-injection indicators found."
        return "{0} indicator(s) found in {1} line(s): {2}.".format(
            len(self.findings),
            len({finding.line for finding in self.findings}),
            ", ".join("{0}x{1}".format(count, kind)
                      for kind, count in sorted(self.counts_by_kind().items())))

    def to_dict(self) -> Dict:
        return {
            "risk": self.risk,
            "kinds": self.kinds,
            "high_kinds": self.high_kinds,
            "counts_by_kind": self.counts_by_kind(),
            "findings": [finding.to_dict() for finding in self.findings],
            "summary": self.summary(),
            "gates_llm": self.blocks_llm,
        }


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def visible(text) -> str:
    """Replace invisible / control characters with ``<U+XXXX>`` markers.

    Used for every excerpt this module emits.  A finding is read by a human in
    a browser, a report, a JSON file and a terminal; it must therefore be
    readable in all four, and must not smuggle the very characters it is
    reporting into any of them.
    """
    out: List[str] = []
    for char in str(text):
        if char == "\t":
            out.append("    ")
            continue
        if char in ZERO_WIDTH_CHARS or char in BIDI_CHARS \
                or char in _INVISIBLE_SINGLE_CHARS or char == "\u00ad" \
                or _TAG_CHAR_RE.match(char) or "\ufe00" <= char <= "\ufe0f" \
                or _CONTROL_RE.match(char) or char == "\x1b" or char in "\r\n":
            out.append("<U+{0:04X}>".format(ord(char)))
            continue
        out.append(char)
    return "".join(out)


def sanitize_for_terminal(text) -> str:
    """Strip ANSI escapes and control characters from text bound for stdout.

    The CLI prints document-derived strings (the SOP title, its version).  An
    ANSI escape in a document title can rewrite a line of terminal output that
    an operator is reading as evidence - "package created" where none was - so
    nothing document-derived is printed without passing through here first.
    Tab and newline are kept - mangling legitimate whitespace would be its own
    bug - but a bare carriage return is not: on a terminal it returns the cursor
    to the start of the line so that whatever follows overwrites what an operator
    just read, which is the same trick as the escape sequences above.
    """
    cleaned = _ANSI_RE.sub("", str(text))
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = cleaned.replace("\r", "")
    cleaned = _ZERO_WIDTH_RE.sub("", cleaned)
    cleaned = _TAG_CHAR_RE.sub("", cleaned)
    cleaned = _INVISIBLE_SINGLE_RE.sub("", cleaned)
    return _BIDI_RE.sub("", cleaned)


def _excerpt(line: str, limit: int = EXCERPT_CHARS) -> str:
    rendered = visible(line.strip())
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit].rstrip() + "…"


def _normalise_lines(lines) -> List[str]:
    """Accept a line list, a single string, or anything iterable of strings."""
    if lines is None:
        return []
    if isinstance(lines, str):
        return lines.split("\n")
    if isinstance(lines, Iterable):
        return [str(line) for line in lines]
    return []


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def _homoglyph_count(line: str) -> int:
    """Look-alike letters sitting inside *mixed-script* words.

    The test is per word, not per line, and that is what keeps a document
    legitimately written in Cyrillic or Greek clean: its words are wholly in one
    script.  An attack has to put a look-alike inside a word the reader reads as
    Latin - "Pr<U+0435>ss the r<U+0435>d button" - and that word then contains
    both ASCII letters and a confusable, which no ordinary document does.
    """
    hits = 0
    for token in _WORD_RE.findall(line):
        confusables = sum(1 for char in token if char in _HOMOGLYPH_LETTERS)
        if not confusables:
            continue
        if any(char.isalpha() and ord(char) < 128 for char in token):
            hits += confusables
    return hits if hits >= HOMOGLYPH_MIN_COUNT else 0


def _is_negated(text: str, start: int) -> bool:
    """Is the match at ``start`` preceded by a negation?

    "Never ignore an audible alarm" and "do not ignore any safety rules" are
    sentences real procedures contain; "ignore all previous instructions" is not.
    The difference is one word in front of the verb, and it is the single most
    useful discriminator in this module.
    """
    window = text[max(0, start - _NEGATION_LOOKBACK):start]
    return bool(_NEGATED_OVERRIDE_RE.search(window))


def _invisible_hit(line: str) -> bool:
    """Invisible or formatting characters other than the zero-width set.

    Tag characters first, because they are the ones that can carry an entire
    instruction.  Soft hyphens need a run and variation selectors need to be
    decorating text rather than an emoji, so that ordinary copied typography does
    not register.
    """
    if _TAG_CHAR_RE.search(line) or _INVISIBLE_SINGLE_RE.search(line):
        return True
    if _SOFT_HYPHEN_RUN_RE.search(line):
        return True
    return bool(_VARIATION_SELECTOR_RE.search(line))


def decode_tag_characters(text) -> str:
    """The ASCII hidden in Unicode TAG characters, if any.

    U+E0041 is a tag "A".  A sentence followed by tag-encoded text looks like the
    sentence alone in every editor, browser and review page - which is the point.
    Exposed because a reviewer asking "what does the hidden text say?" deserves an
    answer, and :func:`scan_document` puts it in the finding's excerpt.
    """
    out = []
    for char in str(text):
        if 0xE0000 <= ord(char) <= 0xE007F:
            code = ord(char) - 0xE0000
            if 0x20 <= code <= 0x7E:
                out.append(chr(code))
    return "".join(out)


def _decode_base64_text(blob: str) -> str:
    """Decode ``blob`` if it is base64 of readable text, else "".

    Deliberately conservative: padding is repaired, decoding must succeed as
    UTF-8, and the result must be long enough and printable enough to be a
    sentence rather than a coincidence.  A hex digest or an identifier decodes to
    noise and is left alone.
    """
    padded = blob + "=" * (-len(blob) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    if len(text) < _BASE64_MIN_DECODED_CHARS:
        return ""
    printable = sum(1 for char in text if char.isprintable() or char in "\t\n\r")
    if printable / float(len(text)) < _BASE64_MIN_PRINTABLE_RATIO:
        return ""
    return text


def scan_document(lines) -> ScanResult:
    """Scan a document for prompt-injection indicators.

    ``lines`` is the document as a list of lines - normally ``SOPContent.lines``,
    i.e. exactly the text the LLM layer would be shown and that citations index
    into - or a single string, which is split on newlines.  Pure, deterministic
    and stdlib-only: the same bytes always give the same ``ScanResult``.
    """
    document = _normalise_lines(lines)
    findings: List[Finding] = []

    def add(number: int, kind: str, line: str) -> None:
        findings.append(Finding(line=number, kind=kind, excerpt=_excerpt(line)))

    for number, line in enumerate(document, 1):
        if not line:
            continue

        seen_kinds = set()
        for kind, pattern in _TEXT_PATTERNS:
            if kind in seen_kinds:
                continue
            match = pattern.search(line)
            if match is None:
                continue
            if kind == KIND_INSTRUCTION_OVERRIDE and _is_negated(line, match.start()):
                continue
            seen_kinds.add(kind)
            add(number, kind, line)

        if _ZERO_WIDTH_RE.search(line):
            add(number, KIND_ZERO_WIDTH, line)
        if _invisible_hit(line):
            hidden = decode_tag_characters(line)
            excerpt = _excerpt(line)
            if hidden:
                excerpt += ' [hidden tag text: "{0}"]'.format(_excerpt(hidden, 60))
            findings.append(Finding(line=number, kind=KIND_INVISIBLE_CHARS,
                                    excerpt=excerpt))
        if _BIDI_RE.search(line):
            add(number, KIND_BIDI_OVERRIDE, line)
        if "\x1b" in line:
            add(number, KIND_ANSI_ESCAPE, line)
        if _CONTROL_RE.search(line):
            add(number, KIND_CONTROL_CHAR, line)
        if _homoglyph_count(line):
            add(number, KIND_HOMOGLYPH, line)

        # Base64: report the blob, and then scan what it decodes to.  An attacker
        # who base64s "ignore all previous instructions" is hiding the phrasing
        # from a reader, not from a decoder, and a reviewer needs to be told which
        # of the two it was.  One blob per line is decoded, matching the
        # one-finding-per-line-per-kind convention everywhere else here.
        blob = _BASE64_RE.search(line)
        if blob is not None:
            add(number, KIND_BASE64_BLOB, line)
            decoded = _decode_base64_text(blob.group(0))
            for kind, pattern in (_TEXT_PATTERNS if decoded else ()):
                hit = pattern.search(decoded)
                if hit is None:
                    continue
                if kind == KIND_INSTRUCTION_OVERRIDE and _is_negated(decoded,
                                                                    hit.start()):
                    continue
                findings.append(Finding(
                    line=number, kind=kind,
                    excerpt="base64-decoded: " + _excerpt(decoded)))

    findings.extend(_repetition_findings(document))

    findings.sort(key=lambda finding: (finding.line, finding.kind))
    risk = RISK_NONE
    if any(finding.kind in HIGH_KINDS for finding in findings):
        risk = RISK_HIGH
    elif findings:
        risk = RISK_LOW
    return ScanResult(risk=risk, findings=tuple(findings))


def _repetition_findings(document: Sequence[str]) -> List[Finding]:
    """Report a substantial line repeated ``REPETITION_THRESHOLD`` times or more.

    Reported once, against its first occurrence, so a line pasted 500 times
    produces one finding rather than 500.
    """
    first_seen: Dict[str, int] = {}
    counts: Dict[str, int] = {}
    for number, line in enumerate(document, 1):
        key = " ".join(line.split())
        if len(key) < REPETITION_MIN_CHARS:
            continue
        counts[key] = counts.get(key, 0) + 1
        first_seen.setdefault(key, number)

    out: List[Finding] = []
    for key, count in counts.items():
        if count >= REPETITION_THRESHOLD:
            out.append(Finding(
                line=first_seen[key], kind=KIND_REPETITION,
                excerpt="{0} (repeated {1} times)".format(_excerpt(key), count)))
    return out


def scan_sop(sop) -> ScanResult:
    """Scan a parsed :class:`src.parser.SOPContent` (or anything with ``lines``).

    Falls back to ``raw_content`` when ``lines`` is absent, mirroring
    :func:`src.llm.grounding.document_lines` so the scan and the model see the
    same text.
    """
    lines = getattr(sop, "lines", None)
    if isinstance(lines, (list, tuple)) and lines:
        return scan_document(list(lines))
    return scan_document(getattr(sop, "raw_content", "") or "")


#: The note the enhancement report carries when the gate refused to run.  One
#: wording, used by both the web app and the CLI, so an SME reading either sees
#: the same sentence and a test can assert on it once.
LLM_SKIP_NOTE_PREFIX = "LLM enhancement skipped: document flagged by injection scan"


def llm_skip_note(result: ScanResult) -> str:
    """The report note explaining why the LLM layer did not run.

    There is deliberately no override in this build: a ``high`` verdict means the
    model is not shown the document, full stop.  An SME-authorised override
    ("I have read the flagged lines and they are a false positive") is a later
    feature and needs an authenticated actor and an audit entry of its own, both
    of which arrive with M2 - see ``docs/SECURITY.md``.
    """
    kinds = ", ".join(result.high_kinds or result.kinds) or "unknown"
    lines = sorted({finding.line for finding in result.findings
                    if finding.kind in HIGH_KINDS})
    return ("{0} ({1}) at line(s) {2}. The deterministic content is unchanged and "
            "is what this package contains; no document text was sent to a "
            "model.".format(
                LLM_SKIP_NOTE_PREFIX, kinds,
                ", ".join(str(number) for number in lines) or "-"))


def result_from_dict(data) -> ScanResult:
    """Rebuild a :class:`ScanResult` from :meth:`ScanResult.to_dict` output.

    Used by the web app and the CLI, which read the scan back out of
    ``job.json`` rather than re-scanning.  Unknown or missing fields degrade to
    an empty ``none`` result - never to an exception, and never to a *lower*
    risk than the stored one.
    """
    if not isinstance(data, dict):
        return ScanResult()
    findings = []
    for entry in data.get("findings") or []:
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry.get("line", 0))
        except (TypeError, ValueError):
            number = 0
        findings.append(Finding(line=number,
                                kind=str(entry.get("kind", "")),
                                excerpt=str(entry.get("excerpt", ""))))
    risk = str(data.get("risk") or "")
    if risk not in RISK_ORDER:
        risk = RISK_HIGH if findings else RISK_NONE
    elif risk == RISK_NONE and findings:
        # A stored result never has findings at `none`; something rewrote it, or
        # wrote it by hand. Fail closed rather than let an edited job.json turn
        # the gate off.
        risk = RISK_HIGH
    return ScanResult(risk=risk, findings=tuple(findings))
