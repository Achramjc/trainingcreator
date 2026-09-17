"""
SOP Parser - Extract content from various document formats
"""

import os
import re
from typing import Dict, List, Optional, Tuple
from pathlib import Path

import markdown


# Section headings recognised in SOP documents. Keys are the normalised
# (lower-cased, numbering- and colon-stripped) heading text; values are the
# canonical section this document is filed under. Any line that reduces to
# one of these keys is treated as a section boundary, and section bodies run
# from just after the heading line to the next recognised heading (of any
# kind).
SECTION_ALIASES = {
    "purpose": "purpose",
    "objective": "purpose",
    "objectives": "purpose",
    "scope": "scope",
    "definitions": "definitions",
    "definition": "definitions",
    "key terms and definitions": "definitions",
    "responsibilities": "responsibilities",
    "responsibility": "responsibilities",
    "safety warnings": "safety",
    "safety": "safety",
    "warnings": "safety",
    "precautions": "safety",
    "safety precautions": "safety",
    "procedure": "procedure",
    "procedures": "procedure",
    "procedure steps": "procedure",
    "references": "references",
    "reference": "references",
    "documentation": "documentation",
    "records": "documentation",
    "revision history": "other",
}

# A heading may be prefixed with a numbering scheme like "1.0 ", "3. " or
# "4.1.2 ".
_NUMBERING_RE = re.compile(r"^(\d+(?:\.\d+)*)[.):]?\s+(.+)$")

# Step headings inside a PROCEDURE section, tried in this order:
#   "Step 2: Activate Emergency Stop" / "Step 3 -" / "Step 4."
#   "4.1.1 Sub-step text"                (three-level numbering; promoted to
#                                          its own step only when it has no
#                                          enclosing "4.1" step - see
#                                          _extract_procedures)
#   "4.1 Power Down the Press"           (numbered sub-section under a
#                                          numbered PROCEDURE heading)
#   "1. Identify Emergency Situation" / "1) ..." / "1: ..."
_STEP_WORD_RE = re.compile(r"^step\s+(\d+)\s*[:.\-]?\s*(.*)$", re.IGNORECASE)
_STEP_SUBDECIMAL_RE = re.compile(r"^(\d+\.\d+\.\d+)\.?\s+(.*)$")
_STEP_DECIMAL_RE = re.compile(r"^(\d+\.\d+)\.?\s+(.*)$")
_STEP_PLAIN_RE = re.compile(r"^(\d+)[.):]\s*(.*)$")

# Lettered sub-steps: "a. ..." / "b) ..."
_SUBSTEP_RE = re.compile(r"^\s*([a-z])[.)]\s+(.*)$")
# Three-level numeric sub-steps ("5.1.1 ...") nested under a "5.1" step.
_NUMERIC_SUBSTEP_RE = re.compile(r"^\s*(\d+\.\d+\.\d+)\.?\s+(.*)$")

_WARNING_MARKER_RE = re.compile(r"^\s*(warning|caution|danger)\s*:\s*(.*)$", re.IGNORECASE)
_STOP_MARKER_RE = re.compile(r"^\s*(warning|caution|danger|note)\s*:", re.IGNORECASE)

_BULLET_RE = re.compile(r"^[\-\*•‣◦⁃]\s*")


def _normalize_ws(text: str) -> str:
    """Collapse all whitespace (including newlines) into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


class SOPContent:
    """Structured representation of an SOP document"""

    def __init__(self):
        self.title: str = ""
        self.version: str = ""
        self.effective_date: str = ""
        self.purpose: str = ""
        self.scope: str = ""
        self.responsibilities: List[str] = []
        self.procedures: List[Dict[str, any]] = []
        self.safety_warnings: List[str] = []
        self.definitions: Dict[str, str] = {}
        self.references: List[str] = []
        self.raw_content: str = ""
        #: 1-indexed, inclusive [start, end] line spans into `self.lines` for
        #: every extracted field that could be traced back to source text.
        #: Keys are omitted when the corresponding field wasn't found.
        #: See the module docstring / CLAUDE.md for the exact key set.
        self.provenance: Dict[str, any] = {}
        #: The exact line list `self.provenance` spans index into -- the text
        #: *after* format conversion (e.g. markdown stripped to plain text),
        #: which is NOT the same as `self.raw_content` for markdown input.
        self.lines: List[str] = []

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            "title": self.title,
            "version": self.version,
            "effective_date": self.effective_date,
            "purpose": self.purpose,
            "scope": self.scope,
            "responsibilities": self.responsibilities,
            "procedures": self.procedures,
            "safety_warnings": self.safety_warnings,
            "definitions": self.definitions,
            "references": self.references,
            "raw_content": self.raw_content,
            "provenance": self.provenance,
            "lines": self.lines,
        }

    def excerpt(self, span, context: int = 0) -> str:
        """
        Return the source text a provenance span points at.

        `span` is a 1-indexed, inclusive [start, end] pair as stored in
        `self.provenance` (or a procedure's `source_lines`). `context` adds
        that many extra lines on either side (clamped to the document). Used
        by the transparency report and the SME review UI to show the cited
        text next to a generated claim.
        """
        if not span or len(span) != 2:
            return ""
        start, end = span
        start_idx = max(0, (start - 1) - context)
        end_idx = min(len(self.lines), end + context)
        return "\n".join(self.lines[start_idx:end_idx])


class SOPParser:
    """Parse SOPs from various document formats"""

    def __init__(self):
        self.supported_formats = ['.txt', '.md', '.pdf', '.docx']

    def parse(self, file_path: str) -> SOPContent:
        """
        Parse an SOP document and extract structured content

        Args:
            file_path: Path to the SOP document

        Returns:
            SOPContent object with extracted information
        """
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        ext = file_path.suffix.lower()

        if ext == '.txt':
            return self._parse_text(file_path)
        elif ext == '.md':
            return self._parse_markdown(file_path)
        elif ext == '.pdf':
            return self._parse_pdf(file_path)
        elif ext == '.docx':
            return self._parse_docx(file_path)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

    def _parse_text(self, file_path: Path) -> SOPContent:
        """Parse plain text SOP"""
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        return self._extract_structure(content)

    def _parse_markdown(self, file_path: Path) -> SOPContent:
        """Parse Markdown SOP"""
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Convert markdown to plain text for processing
        html = markdown.markdown(content)
        # Simple HTML tag removal
        text = re.sub('<[^<]+?>', '', html)

        return self._extract_structure(text, original_content=content)

    def _parse_pdf(self, file_path: Path) -> SOPContent:
        """Parse PDF SOP"""
        try:
            import pdfplumber
        except ImportError:
            raise ImportError("pdfplumber is required for PDF parsing. Install with: pip install pdfplumber")

        text = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"

        return self._extract_structure(text)

    def _parse_docx(self, file_path: Path) -> SOPContent:
        """Parse DOCX SOP"""
        try:
            from docx import Document
        except ImportError:
            raise ImportError("python-docx is required for DOCX parsing. Install with: pip install python-docx")

        doc = Document(file_path)
        text = ""

        for paragraph in doc.paragraphs:
            text += paragraph.text + "\n"

        return self._extract_structure(text)

    # ------------------------------------------------------------------
    # Heading / section detection
    # ------------------------------------------------------------------

    def _match_heading(self, line: str, strict: bool = False) -> Optional[Tuple[str, str]]:
        """
        Test whether `line` is a recognised section heading.

        Returns (canonical_key, heading_text) or None. canonical_key is one
        of the values in SECTION_ALIASES, or 'other' for an unrecognised but
        heading-shaped ALL-CAPS line (e.g. "REVISION HISTORY:") which still
        terminates the previous section's body.

        When `strict` is True, only a known SECTION_ALIASES entry (with or
        without numbering/a trailing colon) counts as a heading -- the
        generic ALL-CAPS fallback is skipped. This is used inside a
        PROCEDURE section's body so a bare ALL-CAPS line that is really part
        of a step (an acronym like "LOTO", a shouted instruction like
        "PRESS THE E-STOP") is not mistaken for a new section.
        """
        stripped = line.strip()
        if not stripped:
            return None

        rest = stripped
        m = _NUMBERING_RE.match(stripped)
        if m:
            rest = m.group(2).strip()

        if not rest:
            return None

        rest_no_colon = rest[:-1].strip() if rest.endswith(':') else rest.strip()
        if not rest_no_colon:
            return None

        key = rest_no_colon.lower()
        if key in SECTION_ALIASES:
            return SECTION_ALIASES[key], rest_no_colon

        if strict:
            return None

        # Generic ALL-CAPS heading fallback (e.g. "REVISION HISTORY").
        words = rest_no_colon.split()
        has_alpha = any(c.isalpha() for c in rest_no_colon)
        if has_alpha and 1 <= len(words) <= 6 and rest_no_colon == rest_no_colon.upper():
            return "other", rest_no_colon

        return None

    def _find_sections(
        self, lines: List[str]
    ) -> Tuple[Dict[str, Tuple[int, int]], List[int], List[int]]:
        """
        Scan all lines for section headings.

        Returns:
            sections: canonical_key -> (body_start_line_idx, body_end_line_idx)
                      (first occurrence wins; body_end is exclusive)
            heading_idxs: sorted list of every heading line index found
                          (including unrecognised 'other' headings), used as
                          hard stop boundaries elsewhere (e.g. wrapped
                          warnings, fallback procedure scanning).
            strict_heading_idxs: sorted list of only the headings that match a
                          known SECTION_ALIASES entry (never the generic
                          ALL-CAPS fallback). Used to bound procedure step
                          bodies, so a bare ALL-CAPS line inside a step does
                          not end it.
        """
        headings = []  # (line_idx, key)
        strict_heading_idxs = []
        for i, line in enumerate(lines):
            match = self._match_heading(line)
            if match:
                key, _text = match
                headings.append((i, key))
            if self._match_heading(line, strict=True):
                strict_heading_idxs.append(i)

        sections: Dict[str, Tuple[int, int]] = {}
        for idx, (line_idx, key) in enumerate(headings):
            body_start = line_idx + 1
            body_end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
            if key != "other" and key not in sections:
                sections[key] = (body_start, body_end)

        heading_idxs = [h[0] for h in headings]
        return sections, heading_idxs, strict_heading_idxs

    # ------------------------------------------------------------------
    # Top-level structure extraction
    # ------------------------------------------------------------------

    def _extract_structure(self, content: str, original_content: str = None) -> SOPContent:
        """
        Extract structured information from text content.

        Extraction is heading-driven: the document is split into sections by
        recognised headings, and fields are pulled from the matching
        section's body. A permissive regex-based fallback is used for
        fields whose heading isn't found, so documents without recognisable
        headings still get *something*.
        """
        sop = SOPContent()
        sop.raw_content = original_content or content

        lines = content.split('\n')
        sop.lines = lines

        # Extract title (usually first non-empty line or line with "SOP" or "Procedure")
        for i, raw_line in enumerate(lines[:10]):
            line = raw_line.strip()
            if line and (not sop.title or 'sop' in line.lower() or 'procedure' in line.lower()):
                sop.title = line
                sop.provenance['title'] = [i + 1, i + 1]
                break

        # Extract version
        version_match = re.search(r'version[:\s]+([0-9.]+)', content, re.IGNORECASE)
        if version_match:
            sop.version = version_match.group(1)
            sop.provenance['version'] = self._offset_span(content, version_match.start(), version_match.end())

        # Extract effective date
        date_match = re.search(r'effective\s+date[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', content, re.IGNORECASE)
        if date_match:
            sop.effective_date = date_match.group(1)
            sop.provenance['effective_date'] = self._offset_span(content, date_match.start(), date_match.end())

        sections, heading_idxs, strict_heading_idxs = self._find_sections(lines)

        # Purpose
        if 'purpose' in sections:
            sop.purpose = self._extract_section_text(lines, sections['purpose'])
            span = self._nonempty_span(lines, *sections['purpose'])
            if span:
                sop.provenance['purpose'] = span
        else:
            purpose_match = re.search(
                r'(?:purpose|objective)[:\s]+(.*?)(?:\n\n|\n[A-Z]|\Z)', content, re.IGNORECASE | re.DOTALL
            )
            if purpose_match:
                sop.purpose = _normalize_ws(purpose_match.group(1))
                if purpose_match.group(1).strip():
                    sop.provenance['purpose'] = self._offset_span(
                        content, purpose_match.start(1), purpose_match.end(1)
                    )

        # Scope
        if 'scope' in sections:
            sop.scope = self._extract_section_text(lines, sections['scope'])
            span = self._nonempty_span(lines, *sections['scope'])
            if span:
                sop.provenance['scope'] = span
        else:
            scope_match = re.search(
                r'scope[:\s]+(.*?)(?:\n\n|\n[A-Z]|\Z)', content, re.IGNORECASE | re.DOTALL
            )
            if scope_match:
                sop.scope = _normalize_ws(scope_match.group(1))
                if scope_match.group(1).strip():
                    sop.provenance['scope'] = self._offset_span(
                        content, scope_match.start(1), scope_match.end(1)
                    )

        # Responsibilities
        if 'responsibilities' in sections:
            sop.responsibilities, resp_spans = self._extract_responsibilities(lines, sections['responsibilities'])
            if resp_spans:
                sop.provenance['responsibilities'] = resp_spans

        # Definitions
        if 'definitions' in sections:
            sop.definitions, def_spans = self._extract_definitions(lines, sections['definitions'])
        else:
            sop.definitions, def_spans = self._extract_definitions_fallback(content)
        if def_spans:
            sop.provenance['definitions'] = def_spans

        # Procedures: use the recognised PROCEDURE section body when present,
        # otherwise fall back to scanning the whole document (still bounded
        # by any recognised heading, so it won't run past e.g. REFERENCES:).
        # Only *strict* (known-alias) headings terminate a step's body here --
        # a bare ALL-CAPS line inside a step (an acronym, a shouted
        # instruction) is not a real section boundary.
        if 'procedure' in sections:
            start, end = sections['procedure']
            sop.procedures = self._extract_procedures(lines, start, end, strict_heading_idxs)
        else:
            sop.procedures = self._extract_procedures(lines, 0, len(lines), strict_heading_idxs)

        # Safety warnings: found anywhere in the document, not just inside a
        # SAFETY WARNINGS section.
        sop.safety_warnings, warning_spans = self._extract_safety_warnings(lines, heading_idxs)
        if warning_spans:
            sop.provenance['safety_warnings'] = warning_spans

        return sop

    # ------------------------------------------------------------------
    # Provenance helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _offset_to_line(content: str, offset: int) -> int:
        """0-indexed line number containing `offset`, consistent with content.split('\\n')."""
        return content.count('\n', 0, offset)

    def _offset_span(self, content: str, start: int, end: int) -> List[int]:
        """1-indexed inclusive [s, e] line span covering content[start:end]."""
        start_line = self._offset_to_line(content, start)
        # `end` may point just past the matched text; back it off by one so a
        # match ending exactly at a newline doesn't spill onto the next line.
        end_line = self._offset_to_line(content, max(start, end - 1))
        return [start_line + 1, end_line + 1]

    @staticmethod
    def _nonempty_span(lines: List[str], start: int, end: int) -> Optional[List[int]]:
        """1-indexed inclusive [s, e] span of the first/last non-blank line in lines[start:end], or None."""
        idxs = [i for i in range(start, end) if lines[i].strip()]
        if not idxs:
            return None
        return [idxs[0] + 1, idxs[-1] + 1]

    def _extract_section_text(self, lines: List[str], section_range: Tuple[int, int]) -> str:
        start, end = section_range
        body_lines = [l for l in lines[start:end] if l.strip()]
        return _normalize_ws(" ".join(body_lines))

    # ------------------------------------------------------------------
    # Responsibilities
    # ------------------------------------------------------------------

    def _extract_responsibilities(
        self, lines: List[str], section_range: Tuple[int, int]
    ) -> Tuple[List[str], List[List[int]]]:
        start, end = section_range
        result = []
        spans = []
        for i in range(start, end):
            line = lines[i].strip()
            if not line:
                continue
            line = _BULLET_RE.sub('', line).strip()
            if line:
                result.append(line)
                spans.append([i + 1, i + 1])
        return result, spans

    # ------------------------------------------------------------------
    # Definitions
    # ------------------------------------------------------------------

    def _extract_definitions(
        self, lines: List[str], section_range: Tuple[int, int]
    ) -> Tuple[Dict[str, str], Dict[str, List[int]]]:
        """
        Extract term definitions from a DEFINITIONS section body.

        Terms may contain parentheses, hyphens, slashes and digits (e.g.
        "Emergency Shutdown (E-Stop)", "Lockout/Tagout (LOTO)"). Each
        definition is expected on its own line as "Term: definition" (or
        "Term - definition").
        """
        start, end = section_range
        definitions: Dict[str, str] = {}
        spans: Dict[str, List[int]] = {}
        for i in range(start, end):
            line = lines[i].strip()
            if not line:
                continue
            term = definition = None
            if ':' in line:
                term, _, definition = line.partition(':')
            elif ' - ' in line:
                term, _, definition = line.partition(' - ')
            if term is not None:
                term = term.strip()
                definition = definition.strip()
                if term and definition:
                    definitions[term] = definition
                    spans[term] = [i + 1, i + 1]
        return definitions, spans

    def _extract_definitions_fallback(self, content: str) -> Tuple[Dict[str, str], Dict[str, List[int]]]:
        """Best-effort definitions extraction when no DEFINITIONS heading was found."""
        definitions: Dict[str, str] = {}
        spans: Dict[str, List[int]] = {}
        def_section = re.search(
            r'definitions?\s*:?\s*\n(.*?)(?:\n[A-Z][A-Za-z /]*:|\Z)', content, re.IGNORECASE | re.DOTALL
        )
        if not def_section:
            return definitions, spans

        group_start_line = self._offset_to_line(content, def_section.start(1))
        for offset, line in enumerate(def_section.group(1).split('\n')):
            line = line.strip()
            if not line:
                continue
            term = definition = None
            if ':' in line:
                term, _, definition = line.partition(':')
            elif ' - ' in line:
                term, _, definition = line.partition(' - ')
            if term is not None:
                term = term.strip()
                definition = definition.strip()
                if term and definition:
                    definitions[term] = definition
                    line_no = group_start_line + offset + 1
                    spans[term] = [line_no, line_no]
        return definitions, spans

    # ------------------------------------------------------------------
    # Safety warnings
    # ------------------------------------------------------------------

    def _extract_safety_warnings(
        self, lines: List[str], heading_idxs: List[int]
    ) -> Tuple[List[str], List[List[int]]]:
        """
        Find WARNING:/CAUTION:/DANGER: markers anywhere in the document
        (NOTE: is not a warning). A warning that wraps onto following lines
        is captured whole, up to a blank line, the next marker, or a section
        heading. Duplicates are removed while preserving first-seen order.
        """
        heading_set = set(heading_idxs)
        warnings: List[str] = []
        spans: List[List[int]] = []
        seen = set()
        n = len(lines)
        i = 0
        while i < n:
            match = _WARNING_MARKER_RE.match(lines[i])
            if match:
                text_parts = [match.group(2).strip()]
                last_idx = i
                j = i + 1
                while j < n:
                    if not lines[j].strip():
                        break
                    if _STOP_MARKER_RE.match(lines[j]):
                        break
                    if j in heading_set:
                        break
                    text_parts.append(lines[j].strip())
                    last_idx = j
                    j += 1
                full = _normalize_ws(" ".join(p for p in text_parts if p))
                if full and full not in seen:
                    seen.add(full)
                    warnings.append(full)
                    spans.append([i + 1, last_idx + 1])
                i = j if j > i else i + 1
            else:
                i += 1
        return warnings, spans

    # ------------------------------------------------------------------
    # Procedures
    # ------------------------------------------------------------------

    def _match_step_heading(self, line: str) -> Optional[Tuple[str, str]]:
        stripped = line.strip()
        if not stripped:
            return None

        m = _STEP_WORD_RE.match(stripped)
        if m:
            return m.group(1), m.group(2).strip()

        m = _STEP_SUBDECIMAL_RE.match(stripped)
        if m:
            return m.group(1), m.group(2).strip()

        m = _STEP_DECIMAL_RE.match(stripped)
        if m:
            return m.group(1), m.group(2).strip()

        m = _STEP_PLAIN_RE.match(stripped)
        if m:
            return m.group(1), m.group(2).strip()

        return None

    def _extract_substeps_and_body(self, body_lines: List[str]) -> Tuple[List[str], str]:
        """
        Split a step's body lines into (substeps, full_body_text).

        Lettered sub-steps (a./b./c. or a)/b)/c)) and three-level numeric
        sub-steps ("5.1.1 ...", nested under this step's own "5.1") are
        pulled out into `substeps`, one entry per item (continuation lines
        that follow a sub-step marker, up to the next marker, are folded
        in). `full_body_text` is the whole body -- narrative paragraph(s)
        plus sub-step text with the markers stripped -- whitespace
        normalised into a single line, per the parser's output contract.
        """
        substeps: List[str] = []
        current: Optional[str] = None
        full_parts: List[str] = []

        for line in body_lines:
            m = _SUBSTEP_RE.match(line) or _NUMERIC_SUBSTEP_RE.match(line)
            if m:
                if current is not None:
                    substeps.append(_normalize_ws(current))
                current = m.group(2)
                full_parts.append(m.group(2))
            else:
                if current is not None and line.strip():
                    current += " " + line.strip()
                full_parts.append(line)

        if current is not None:
            substeps.append(_normalize_ws(current))

        body_text = _normalize_ws(" ".join(full_parts))
        return substeps, body_text

    def _extract_procedures(
        self, lines: List[str], start: int, end: int, heading_idxs: List[int]
    ) -> List[Dict[str, any]]:
        """
        Extract procedure steps from lines[start:end].

        Each step captures its full body -- narrative text and lettered or
        three-level-numeric sub-steps -- up to the next step heading or the
        next section heading (e.g. an ALL-CAPS heading line like
        "DOCUMENTATION:" or "REFERENCES:"), across blank lines.

        A three-level numbered line ("4.1.1 ...") is treated as a sub-step
        of its enclosing "4.1" step when one precedes it in this range
        (appended to that step's `substeps`, like a lettered "a." item);
        with no such enclosing step it is promoted to a step of its own,
        numbered "4.1.1".
        """
        candidates = []  # (line_idx, step_number, title)
        for i in range(start, end):
            match = self._match_step_heading(lines[i])
            if match:
                step_num, title = match
                candidates.append((i, step_num, title))

        if not candidates:
            return []

        # Two levels of numbering ("4", "4.1", "Step 4") can bound a step;
        # three-level numbers ("4.1.1") are resolved below against these.
        base_heads = [c for c in candidates if c[1].count(".") < 2]
        three_level = [c for c in candidates if c[1].count(".") == 2]

        base_boundary_set = {c[0] for c in base_heads}
        base_boundary_set.update(idx for idx in heading_idxs if start <= idx < end)
        base_boundary_set.add(end)
        base_boundaries = sorted(base_boundary_set)

        def enclosing_base_step(idx):
            """The base step whose body contains line `idx`, or None."""
            enclosing = None
            for c in base_heads:
                if c[0] < idx:
                    enclosing = c
                else:
                    break
            if enclosing is None:
                return None
            body_end = end
            for b in base_boundaries:
                if b > enclosing[0]:
                    body_end = b
                    break
            return enclosing if enclosing[0] < idx < body_end else None

        promoted = []
        for (i, step_num, title) in three_level:
            prefix = step_num.rsplit(".", 1)[0]
            enclosing = enclosing_base_step(i)
            if enclosing is not None and enclosing[1] == prefix:
                continue  # absorbed as a sub-step of its enclosing N.M step
            promoted.append((i, step_num, title))

        step_heads = sorted(base_heads + promoted, key=lambda c: c[0])

        boundary_set = {h[0] for h in step_heads}
        boundary_set.update(idx for idx in heading_idxs if start <= idx < end)
        boundary_set.add(end)
        boundaries = sorted(boundary_set)

        procedures = []
        for i, step_num, title in step_heads:
            next_boundary = end
            for b in boundaries:
                if b > i:
                    next_boundary = b
                    break

            body_lines = lines[i + 1:next_boundary]

            last_content_idx = i
            for j in range(next_boundary - 1, i, -1):
                if lines[j].strip():
                    last_content_idx = j
                    break

            substeps, body_text = self._extract_substeps_and_body(body_lines)
            content = f"{title}\n{body_text}".strip() if title else body_text

            procedures.append({
                "step_number": step_num,
                "title": title,
                "body": body_text,
                "content": content,
                "substeps": substeps,
                "source_lines": [i + 1, last_content_idx + 1],
            })

        return procedures
