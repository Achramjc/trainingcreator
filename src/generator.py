"""
Training Content Generator - Convert SOP content into structured training modules
"""

import html
from typing import Dict, List
from .parser import SOPContent


class TrainingModule:
    """Structured training module ready for export"""

    def __init__(self):
        self.title: str = ""
        self.learning_objectives: List[str] = []
        #: Structured, Bloom's-aligned objectives. Each entry is
        #: {"text": str, "bloom_level": str, "verb": str,
        #:  "source_ref": {"kind": str, "span": [s, e],
        #:                 "step_number": str|None, "term": str|None}}.
        #: `learning_objectives` above is kept in sync as
        #: [o["text"] for o in objectives] for backward compatibility.
        self.objectives: List[Dict] = []
        self.sections: List[Dict] = []
        self.estimated_duration: int = 0  # in minutes
        self.prerequisites: List[str] = []
        self.summary: str = ""

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            "title": self.title,
            "learning_objectives": self.learning_objectives,
            "objectives": self.objectives,
            "sections": self.sections,
            "estimated_duration": self.estimated_duration,
            "prerequisites": self.prerequisites,
            "summary": self.summary,
        }


class TrainingGenerator:
    """Generate training content from SOP documents"""

    def __init__(self):
        self.words_per_minute = 200  # Average reading speed

    def generate(self, sop_content: SOPContent) -> TrainingModule:
        """
        Generate a complete training module from SOP content

        Args:
            sop_content: Parsed SOP content

        Returns:
            TrainingModule ready for export
        """
        module = TrainingModule()

        # Set basic information
        module.title = sop_content.title or "Training Module"

        # Generate Bloom's-aligned learning objectives, each traceable back
        # to a source-line span. `learning_objectives` is kept as the flat
        # list of texts for backward compatibility.
        module.objectives = self._generate_objectives(sop_content)
        module.learning_objectives = [o["text"] for o in module.objectives]

        # Create training sections
        module.sections = self._create_sections(sop_content)

        # Estimate duration
        module.estimated_duration = self._estimate_duration(sop_content)

        # Generate summary
        module.summary = self._generate_summary(sop_content)

        return module

    def _truncate_at_word(self, text: str, max_len: int) -> str:
        """Truncate text to at most max_len characters, breaking on a word boundary (no ellipsis)."""
        text = (text or "").strip()
        if len(text) <= max_len:
            return text
        truncated = text[:max_len].rsplit(' ', 1)[0]
        return truncated or text[:max_len]

    #: Bloom's level and verb used for each kind of source material. No LLM
    #: involved -- purely deterministic templates over parsed, cited content.
    _BLOOM = {
        "definition": ("Remember", "Define"),
        "purpose": ("Understand", "Explain"),
        "scope": ("Analyze", "Determine"),
        "safety": ("Evaluate", "Identify"),
        "step": ("Apply", "Perform"),
    }

    MAX_OBJECTIVES = 8

    @staticmethod
    def _format_term_list(terms: List[str]) -> str:
        """"A, B and C" -- no Oxford comma, matching SME-reviewed phrasing."""
        if len(terms) == 1:
            return terms[0]
        if len(terms) == 2:
            return f"{terms[0]} and {terms[1]}"
        return ", ".join(terms[:-1]) + f" and {terms[-1]}"

    def _step_label(self, proc: Dict, max_len: int = 60) -> str:
        title = (proc.get('title') or '').strip()
        if title:
            return title
        body = proc.get('body') or proc.get('content', '')
        return self._truncate_at_word(body, max_len)

    def _single_step_objective(self, proc: Dict) -> Dict:
        bloom_level, verb = self._BLOOM["step"]
        number = proc.get('step_number')
        title = self._step_label(proc)
        return {
            "text": f"{verb} Step {number}: {title}",
            "bloom_level": bloom_level,
            "verb": verb,
            "source_ref": {
                "kind": "step",
                "span": proc["source_lines"],
                "step_number": str(number) if number is not None else None,
                "term": None,
            },
        }

    def _grouped_step_objectives(self, steps: List[Dict], n_groups: int) -> List[Dict]:
        """Split `steps` into `n_groups` contiguous, near-equal chunks and
        return one Apply objective per chunk (a chunk of size 1 is rendered
        exactly like a single-step objective, not as a degenerate range)."""
        n = len(steps)
        n_groups = max(1, min(n_groups, n))
        base, remainder = divmod(n, n_groups)

        bloom_level, verb = self._BLOOM["step"]
        result = []
        idx = 0
        for i in range(n_groups):
            size = base + (1 if i < remainder else 0)
            if size <= 0:
                continue
            chunk = steps[idx:idx + size]
            idx += size

            if len(chunk) == 1:
                result.append(self._single_step_objective(chunk[0]))
                continue

            first, last = chunk[0], chunk[-1]
            first_num, last_num = first.get('step_number'), last.get('step_number')
            span = [first["source_lines"][0], last["source_lines"][1]]
            text = (
                f"{verb} Steps {first_num}–{last_num}: "
                f"{self._step_label(first, 40)} … {self._step_label(last, 40)}"
            )
            result.append({
                "text": text,
                "bloom_level": bloom_level,
                "verb": verb,
                "source_ref": {
                    "kind": "step",
                    "span": span,
                    "step_number": f"{first_num}–{last_num}",
                    "term": None,
                },
            })
        return result

    def _cap_objectives(self, objectives: List[Dict]) -> List[Dict]:
        """Last-resort cap at MAX_OBJECTIVES, preserving order and always
        keeping a safety objective if one was generated. The budgeting in
        `_generate_objectives` keeps this from triggering in practice --
        this only guards pathological inputs (e.g. very many definitions on
        a document with almost no procedure)."""
        if len(objectives) <= self.MAX_OBJECTIVES:
            return objectives
        safety = next((o for o in objectives if o["source_ref"]["kind"] == "safety"), None)
        kept = objectives[: self.MAX_OBJECTIVES]
        if safety and safety not in kept:
            kept = objectives[: self.MAX_OBJECTIVES - 1] + [safety]
        return kept

    def _purpose_objective(self, sop_content: SOPContent, provenance: Dict) -> Dict:
        purpose_span = provenance.get("purpose")
        if not (sop_content.purpose and purpose_span):
            return None
        bloom_level, verb = self._BLOOM["purpose"]
        return {
            "text": f"{verb} why this procedure exists: "
                    f"{self._truncate_at_word(sop_content.purpose, 100)}",
            "bloom_level": bloom_level,
            "verb": verb,
            "source_ref": {"kind": "purpose", "span": purpose_span, "step_number": None, "term": None},
        }

    def _scope_objective(self, sop_content: SOPContent, provenance: Dict) -> Dict:
        scope_span = provenance.get("scope")
        if not (sop_content.scope and scope_span):
            return None
        bloom_level, verb = self._BLOOM["scope"]
        return {
            "text": f"{verb} when this procedure applies and when it does not",
            "bloom_level": bloom_level,
            "verb": verb,
            "source_ref": {"kind": "scope", "span": scope_span, "step_number": None, "term": None},
        }

    def _safety_objective(self, sop_content: SOPContent, provenance: Dict) -> Dict:
        warning_spans = provenance.get("safety_warnings") or []
        if not (sop_content.safety_warnings and warning_spans):
            return None
        bloom_level, verb = self._BLOOM["safety"]
        safety_span = [min(s[0] for s in warning_spans), max(s[1] for s in warning_spans)]
        return {
            "text": f"{verb} each hazard in this procedure and the precaution it requires",
            "bloom_level": bloom_level,
            "verb": verb,
            "source_ref": {"kind": "safety", "span": safety_span, "step_number": None, "term": None},
        }

    def _definition_objectives(self, sop_content: SOPContent, provenance: Dict) -> List[Dict]:
        """Remember-level objective(s) for definitions.

        Definitions are the least valuable objective type for an SME
        reviewer -- with more than two terms they are collapsed into a
        single combined objective so they don't crowd out step coverage;
        with one or two terms the per-term form is kept.
        """
        bloom_level, verb = self._BLOOM["definition"]
        def_spans = provenance.get("definitions") or {}
        terms = [t for t in sop_content.definitions if t in def_spans]
        if not terms:
            return []

        if len(terms) <= 2:
            return [
                {
                    "text": f"{verb} {term}",
                    "bloom_level": bloom_level,
                    "verb": verb,
                    "source_ref": {"kind": "definition", "span": def_spans[term], "step_number": None, "term": term},
                }
                for term in terms
            ]

        spans = [def_spans[t] for t in terms]
        combined_span = [min(s[0] for s in spans), max(s[1] for s in spans)]
        return [{
            "text": f"{verb} the key terms used in this procedure: {self._format_term_list(terms)}",
            "bloom_level": bloom_level,
            "verb": verb,
            "source_ref": {"kind": "definitions", "span": combined_span, "step_number": None, "term": None},
        }]

    def _step_objectives(self, steps: List[Dict], budget: int) -> List[Dict]:
        """Apply-level objectives for the procedure, filling up to `budget`
        slots: one objective per step when they fit, otherwise contiguous
        near-equal groups -- never fewer than 2 step objectives when there
        are at least 2 steps."""
        if not steps:
            return []
        if len(steps) <= budget:
            return [self._single_step_objective(p) for p in steps]

        n_groups = max(1, min(budget, len(steps)))
        if len(steps) >= 2:
            n_groups = max(2, n_groups)
        return self._grouped_step_objectives(steps, n_groups)

    def _generate_objectives(self, sop_content: SOPContent) -> List[Dict]:
        """Generate Bloom's-aligned learning objectives, each carrying a
        `source_ref` that resolves to a valid span in `sop_content.lines`.

        Presentation order (and priority for the 8-objective budget) puts
        the substance of the training first: purpose (Understand), scope
        (Analyze), safety (Evaluate), the procedure steps (Apply), and
        definitions (Remember) last -- steps get whatever budget remains
        after the other categories, since they are what an SME actually
        cares about an operator being able to do.
        """
        provenance = sop_content.provenance or {}

        purpose_obj = self._purpose_objective(sop_content, provenance)
        scope_obj = self._scope_objective(sop_content, provenance)
        safety_obj = self._safety_objective(sop_content, provenance)
        lead_objectives = [o for o in (purpose_obj, scope_obj, safety_obj) if o]

        definition_objs = self._definition_objectives(sop_content, provenance)

        steps = [p for p in sop_content.procedures if p.get("source_lines")]
        non_step_count = len(lead_objectives) + len(definition_objs)
        budget = max(1, self.MAX_OBJECTIVES - non_step_count)
        step_objs = self._step_objectives(steps, budget)

        objectives = lead_objectives + step_objs + definition_objs
        return self._cap_objectives(objectives)

    @staticmethod
    def _span_ref(kind: str, span, step_number: str = None, term: str = None) -> Dict:
        return {"kind": kind, "span": span, "step_number": step_number, "term": term}

    def _intro_citations(self, sop_content: SOPContent) -> List[Dict]:
        provenance = sop_content.provenance or {}
        citations = []
        for kind in ("title", "version", "effective_date", "purpose", "scope"):
            span = provenance.get(kind)
            if span:
                citations.append(self._span_ref(kind, span))
        for span in provenance.get("responsibilities") or []:
            citations.append(self._span_ref("responsibilities", span))
        return citations

    def _safety_citations(self, sop_content: SOPContent) -> List[Dict]:
        return [self._span_ref("safety", span) for span in (sop_content.provenance or {}).get("safety_warnings") or []]

    def _definitions_citations(self, sop_content: SOPContent) -> List[Dict]:
        spans = (sop_content.provenance or {}).get("definitions") or {}
        return [self._span_ref("definition", span, term=term) for term, span in spans.items()]

    def _procedures_citations(self, sop_content: SOPContent) -> List[Dict]:
        return [
            self._span_ref("step", proc["source_lines"], step_number=str(proc.get("step_number")))
            for proc in sop_content.procedures
            if proc.get("source_lines")
        ]

    def _summary_citations(self, sop_content: SOPContent) -> List[Dict]:
        span = (sop_content.provenance or {}).get("purpose")
        return [self._span_ref("purpose", span)] if span else []

    def _create_sections(self, sop_content: SOPContent) -> List[Dict]:
        """Create structured training sections"""
        sections = []

        # Section 1: Introduction
        intro_section = {
            "id": "intro",
            "title": "Introduction",
            "type": "content",
            "content": self._create_introduction(sop_content),
            "order": 1,
            "citations": self._intro_citations(sop_content),
        }
        sections.append(intro_section)

        # Section 2: Safety and Precautions (if applicable)
        if sop_content.safety_warnings:
            safety_section = {
                "id": "safety",
                "title": "Safety and Precautions",
                "type": "content",
                "content": self._create_safety_section(sop_content),
                "order": 2,
                "critical": True,
                "citations": self._safety_citations(sop_content),
            }
            sections.append(safety_section)

        # Section 3: Definitions (if applicable)
        if sop_content.definitions:
            definitions_section = {
                "id": "definitions",
                "title": "Key Terms and Definitions",
                "type": "content",
                "content": self._create_definitions_section(sop_content),
                "order": 3,
                "citations": self._definitions_citations(sop_content),
            }
            sections.append(definitions_section)

        # Section 4: Procedures (main content)
        procedures_section = {
            "id": "procedures",
            "title": "Procedure Steps",
            "type": "content",
            "content": self._create_procedures_section(sop_content),
            "order": 4,
            "citations": self._procedures_citations(sop_content),
        }
        sections.append(procedures_section)

        # Section 5: Summary and Key Points
        summary_section = {
            "id": "summary",
            "title": "Summary and Key Points",
            "type": "content",
            "content": self._create_summary_section(sop_content),
            "order": 5,
            "citations": self._summary_citations(sop_content),
        }
        sections.append(summary_section)

        return sections

    def _create_introduction(self, sop_content: SOPContent) -> str:
        """Create introduction section content"""
        content_parts = [
            f"<h2>Welcome to the Training Module: {html.escape(sop_content.title)}</h2>"
        ]

        if sop_content.version:
            content_parts.append(f"<p><strong>Version:</strong> {html.escape(sop_content.version)}</p>")

        if sop_content.effective_date:
            content_parts.append(f"<p><strong>Effective Date:</strong> {html.escape(sop_content.effective_date)}</p>")

        if sop_content.purpose:
            content_parts.append(f"<h3>Purpose</h3><p>{html.escape(sop_content.purpose)}</p>")

        if sop_content.scope:
            content_parts.append(f"<h3>Scope</h3><p>{html.escape(sop_content.scope)}</p>")

        if sop_content.responsibilities:
            content_parts.append("<h3>Responsibilities</h3><ul>")
            for resp in sop_content.responsibilities:
                content_parts.append(f"<li>{html.escape(resp)}</li>")
            content_parts.append("</ul>")

        return "\n".join(content_parts)

    def _create_safety_section(self, sop_content: SOPContent) -> str:
        """Create safety and precautions section"""
        content_parts = [
            "<h2>Safety and Precautions</h2>",
            "<div class='safety-alert'>",
            "<p><strong>⚠️ IMPORTANT:</strong> Please read and understand all safety warnings before proceeding.</p>",
            "</div>",
            "<ul class='safety-warnings'>"
        ]

        for warning in sop_content.safety_warnings:
            content_parts.append(f"<li><strong>WARNING:</strong> {html.escape(warning)}</li>")

        content_parts.append("</ul>")

        return "\n".join(content_parts)

    def _create_definitions_section(self, sop_content: SOPContent) -> str:
        """Create definitions section"""
        content_parts = [
            "<h2>Key Terms and Definitions</h2>",
            "<dl class='definitions'>"
        ]

        for term, definition in sop_content.definitions.items():
            content_parts.append(f"<dt><strong>{html.escape(term)}</strong></dt>")
            content_parts.append(f"<dd>{html.escape(definition)}</dd>")

        content_parts.append("</dl>")

        return "\n".join(content_parts)

    def _create_procedures_section(self, sop_content: SOPContent) -> str:
        """Create procedures section with step-by-step instructions"""
        content_parts = [
            "<h2>Procedure Steps</h2>",
            "<p>Follow these steps carefully to complete the procedure:</p>",
            "<ol class='procedure-steps'>"
        ]

        for proc in sop_content.procedures:
            step_num = html.escape(str(proc.get('step_number', '')))
            title = html.escape((proc.get('title') or '').strip())
            body = html.escape(proc.get('body') or proc.get('content', ''))
            substeps = proc.get('substeps', [])

            label = f"Step {step_num}: {title}" if title else f"Step {step_num}"
            content_parts.append(f"<li><strong>{label}</strong>")

            if body:
                content_parts.append(f"<p>{body}</p>")

            if substeps:
                content_parts.append("<ol type='a'>")
                for substep in substeps:
                    content_parts.append(f"<li>{html.escape(substep)}</li>")
                content_parts.append("</ol>")

            content_parts.append("</li>")

        content_parts.append("</ol>")

        return "\n".join(content_parts)

    def _create_summary_section(self, sop_content: SOPContent) -> str:
        """Create summary section"""
        content_parts = [
            "<h2>Summary and Key Points</h2>",
            "<p>Let's review the key points from this training:</p>",
            "<ul class='key-points'>"
        ]

        # Add key points based on procedures
        if sop_content.purpose:
            content_parts.append(
                f"<li>The purpose of this procedure is: {html.escape(self._truncate_at_word(sop_content.purpose, 150))}</li>"
            )

        if sop_content.procedures:
            content_parts.append(f"<li>This procedure consists of {len(sop_content.procedures)} main steps</li>")

        if sop_content.safety_warnings:
            content_parts.append(f"<li>There are {len(sop_content.safety_warnings)} critical safety warnings to remember</li>")

        content_parts.append("</ul>")
        content_parts.append("<p><strong>Next:</strong> Complete the verification assessment to test your knowledge.</p>")

        return "\n".join(content_parts)

    def _generate_summary(self, sop_content: SOPContent) -> str:
        """Generate overall module summary"""
        summary_parts = []

        if sop_content.purpose:
            summary_parts.append(sop_content.purpose)

        summary_parts.append(f"This training covers {len(sop_content.procedures)} main procedure steps.")

        if sop_content.safety_warnings:
            summary_parts.append(f"Includes {len(sop_content.safety_warnings)} critical safety warnings.")

        return " ".join(summary_parts)

    def _estimate_duration(self, sop_content: SOPContent) -> int:
        """Estimate training duration in minutes"""
        word_count = len(sop_content.raw_content.split())
        reading_time = word_count / self.words_per_minute

        # Add time for procedures (1 minute per step minimum)
        procedure_time = len(sop_content.procedures) * 1

        # Add time for assessment (assume 1 minute per question, ~5 questions)
        assessment_time = 5

        total_minutes = int(reading_time + procedure_time + assessment_time)

        return max(total_minutes, 5)  # Minimum 5 minutes
