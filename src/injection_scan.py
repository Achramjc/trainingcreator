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
    obfuscated text (zero-width, bidi override, homoglyphs, base64 blob, ANSI
    escape, stray control characters).  Callers gate the LLM layer on this.
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
    stay at ``"none"``; ``tests/test_injection.py`` asserts it, because a scanner
    that cries wolf on a real SOP would just get switched off.

The deterministic pipeline runs regardless of the verdict.  It is regex and
string slicing: it cannot be talked into anything, its output is escaped
everywhere it is rendered, and refusing to generate training from a flagged
document would only hand an attacker a denial-of-service.  The *LLM* layer is
what gets gated, because a model is the only component here that can be
persuaded.
"""

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
# Every pattern below is tuned against the eight documents in this repository:
# zero findings on all of them is a hard requirement (tests/test_injection.py),
# because a scanner that fires on a real SOP gets turned off.  That tuning is
# why some patterns are narrower than their description: "ignore" needs an
# instruction-ish object, "reveal" needs a prompt-ish object, and a bare
# ALL-CAPS heading or a markdown `###` heading is not a role fence.
# ---------------------------------------------------------------------------
_TEXT_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    # -- instruction-to-AI phrasings ---------------------------------------
    (KIND_INSTRUCTION_OVERRIDE, re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass|skip)\b[^.\n]{0,40}?"
        r"\b(?:instruction|instructions|prompt|prompts|system\s+message|"
        r"guardrails?|restrictions?)\b",
        re.IGNORECASE)),
    (KIND_INSTRUCTION_OVERRIDE, re.compile(
        r"\b(?:ignore|disregard|forget)\b[^.\n]{0,20}?\b(?:the\s+)?"
        r"(?:above|preceding|foregoing|everything\s+above)\b",
        re.IGNORECASE)),
    (KIND_INSTRUCTION_OVERRIDE, re.compile(
        r"\bnew\s+(?:instructions?|directive|task|rules)\s*[:\-]", re.IGNORECASE)),
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\byou\s+are\s+(?:now\b|no\s+longer\b|an?\s+(?:ai\b|assistant\b|"
        r"language\s+model\b|helpful\b|expert\b|chatbot\b|bot\b))",
        re.IGNORECASE)),
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\b(?:act|behave|respond|answer)\s+as\s+(?:an?\s+|if\s+you\s+)",
        re.IGNORECASE)),
    (KIND_ROLE_ASSIGNMENT, re.compile(
        r"\b(?:pretend|roleplay|role-play)\b", re.IGNORECASE)),
    (KIND_AI_ADDRESSED, re.compile(
        r"\bas\s+an?\s+(?:ai|a\.i\.|llm|language\s+model|chatbot)\b",
        re.IGNORECASE)),
    (KIND_AI_ADDRESSED, re.compile(
        r"\b(?:ai|assistant|model|chatbot|claude|chatgpt|gpt)\s*[,:]\s*(?:please\s+)?"
        r"(?:ignore|note|include|add|remember|output|write|say|do)\b",
        re.IGNORECASE)),
    (KIND_AI_ADDRESSED, re.compile(
        r"\b(?:dear|hey|hello)\s+(?:ai|assistant|language\s+model|claude|chatgpt)\b",
        re.IGNORECASE)),
    (KIND_PROMPT_DISCLOSURE, re.compile(
        r"\bsystem\s+prompt\b", re.IGNORECASE)),
    (KIND_PROMPT_DISCLOSURE, re.compile(
        r"\b(?:reveal|disclose|print|output|repeat|show|expose|leak|dump)\b"
        r"[^.\n]{0,30}?\b(?:your|the|these|its)\s+"
        r"(?:system\s+|hidden\s+|initial\s+|original\s+)?"
        r"(?:prompt|prompts|instructions|guidelines|configuration|context)\b",
        re.IGNORECASE)),
    (KIND_PROMPT_DISCLOSURE, re.compile(
        r"\b(?:do\s*n[o']?t|never|don't)\s+(?:tell|mention|reveal|disclose|show)\b"
        r"[^.\n]{0,30}?\b(?:the\s+)?(?:user|reviewer|human|reader|anyone|sme)\b",
        re.IGNORECASE)),
    (KIND_PROMPT_DISCLOSURE, re.compile(
        r"\bkeep\s+th(?:is|ese)\s+(?:instructions?|note|text|secret)\b"
        r"[^.\n]{0,20}?\b(?:secret|hidden|to\s+yourself)\b",
        re.IGNORECASE)),
    # -- directives about the generated training ---------------------------
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\bwhen\s+(?:you\s+)?"
        r"(?:summaris\w+|summariz\w+|generat\w+|creat\w+|writ\w+|produc\w+|"
        r"paraphras\w+|translat\w+|paraphrase)\b[^.\n]{0,40}?"
        r"\b(?:this|the)\s+(?:document|sop|procedure|text|training|content)\b",
        re.IGNORECASE)),
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\b(?:when|while|if)\s+(?:you\s+are\s+)?"
        r"(?:generating|creating|writing|producing|building|drafting)\s+"
        r"(?:the\s+|any\s+)?(?:training|course|quiz|question|questions|"
        r"assessment|summary|summaries|objectives?)\b",
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
    (KIND_GENERATION_DIRECTIVE, re.compile(
        r"\binclude\s+(?:the\s+|this\s+)?(?:link|url|phone\s+number|contact|"
        r"address)\b[^.\n]{0,40}?\b(?:in\s+(?:the|every|each|any)\s+"
        r"(?:summary|summaries|training|course|objective|objectives|question|"
        r"questions|answer|answers|module|content)|in\s+your\s+\w+)",
        re.IGNORECASE)),
    # -- prompt-format delimiters and role fences --------------------------
    (KIND_PROMPT_DELIMITER, re.compile(r"<\|[^\n]{0,40}?\|?>")),
    (KIND_PROMPT_DELIMITER, re.compile(r"\[/?INST\]|\[/?SYS\]|<</?SYS>>")),
    (KIND_PROMPT_DELIMITER, re.compile(r"<\|?(?:im_start|im_end|endoftext)\|?>")),
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
            if pattern.search(line):
                seen_kinds.add(kind)
                add(number, kind, line)

        if _ZERO_WIDTH_RE.search(line):
            add(number, KIND_ZERO_WIDTH, line)
        if _BIDI_RE.search(line):
            add(number, KIND_BIDI_OVERRIDE, line)
        if "\x1b" in line:
            add(number, KIND_ANSI_ESCAPE, line)
        if _CONTROL_RE.search(line):
            add(number, KIND_CONTROL_CHAR, line)
        if _BASE64_RE.search(line):
            add(number, KIND_BASE64_BLOB, line)
        if _homoglyph_count(line):
            add(number, KIND_HOMOGLYPH, line)

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
