"""
Training Content Generator - Convert SOP content into structured training modules
"""

from typing import Dict, List
from .parser import SOPContent


class TrainingModule:
    """Structured training module ready for export"""

    def __init__(self):
        self.title: str = ""
        self.learning_objectives: List[str] = []
        self.sections: List[Dict] = []
        self.estimated_duration: int = 0  # in minutes
        self.prerequisites: List[str] = []
        self.summary: str = ""

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            "title": self.title,
            "learning_objectives": self.learning_objectives,
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

        # Generate learning objectives
        module.learning_objectives = self._generate_learning_objectives(sop_content)

        # Create training sections
        module.sections = self._create_sections(sop_content)

        # Estimate duration
        module.estimated_duration = self._estimate_duration(sop_content)

        # Generate summary
        module.summary = self._generate_summary(sop_content)

        return module

    def _generate_learning_objectives(self, sop_content: SOPContent) -> List[str]:
        """Generate learning objectives based on SOP content"""
        objectives = []

        # Add objective based on purpose
        if sop_content.purpose:
            objectives.append(f"Understand the purpose and importance of this procedure: {sop_content.purpose[:100]}")

        # Add objectives for each major procedure
        for i, proc in enumerate(sop_content.procedures[:5], 1):
            # Extract key action from procedure content
            content = proc.get('content', '')[:80]
            objectives.append(f"Successfully perform step {proc.get('step_number', i)}: {content}...")

        # Add safety objective if warnings exist
        if sop_content.safety_warnings:
            objectives.append("Identify and understand all safety warnings and precautions")

        # Add scope-based objective
        if sop_content.scope:
            objectives.append(f"Recognize when this procedure applies: {sop_content.scope[:100]}")

        return objectives

    def _create_sections(self, sop_content: SOPContent) -> List[Dict]:
        """Create structured training sections"""
        sections = []

        # Section 1: Introduction
        intro_section = {
            "id": "intro",
            "title": "Introduction",
            "type": "content",
            "content": self._create_introduction(sop_content),
            "order": 1
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
                "critical": True
            }
            sections.append(safety_section)

        # Section 3: Definitions (if applicable)
        if sop_content.definitions:
            definitions_section = {
                "id": "definitions",
                "title": "Key Terms and Definitions",
                "type": "content",
                "content": self._create_definitions_section(sop_content),
                "order": 3
            }
            sections.append(definitions_section)

        # Section 4: Procedures (main content)
        procedures_section = {
            "id": "procedures",
            "title": "Procedure Steps",
            "type": "content",
            "content": self._create_procedures_section(sop_content),
            "order": 4
        }
        sections.append(procedures_section)

        # Section 5: Summary and Key Points
        summary_section = {
            "id": "summary",
            "title": "Summary and Key Points",
            "type": "content",
            "content": self._create_summary_section(sop_content),
            "order": 5
        }
        sections.append(summary_section)

        return sections

    def _create_introduction(self, sop_content: SOPContent) -> str:
        """Create introduction section content"""
        content_parts = [
            f"<h2>Welcome to the Training Module: {sop_content.title}</h2>"
        ]

        if sop_content.version:
            content_parts.append(f"<p><strong>Version:</strong> {sop_content.version}</p>")

        if sop_content.effective_date:
            content_parts.append(f"<p><strong>Effective Date:</strong> {sop_content.effective_date}</p>")

        if sop_content.purpose:
            content_parts.append(f"<h3>Purpose</h3><p>{sop_content.purpose}</p>")

        if sop_content.scope:
            content_parts.append(f"<h3>Scope</h3><p>{sop_content.scope}</p>")

        if sop_content.responsibilities:
            content_parts.append("<h3>Responsibilities</h3><ul>")
            for resp in sop_content.responsibilities:
                content_parts.append(f"<li>{resp}</li>")
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
            content_parts.append(f"<li><strong>WARNING:</strong> {warning}</li>")

        content_parts.append("</ul>")

        return "\n".join(content_parts)

    def _create_definitions_section(self, sop_content: SOPContent) -> str:
        """Create definitions section"""
        content_parts = [
            "<h2>Key Terms and Definitions</h2>",
            "<dl class='definitions'>"
        ]

        for term, definition in sop_content.definitions.items():
            content_parts.append(f"<dt><strong>{term}</strong></dt>")
            content_parts.append(f"<dd>{definition}</dd>")

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
            step_num = proc.get('step_number', '')
            content = proc.get('content', '')
            substeps = proc.get('substeps', [])

            content_parts.append(f"<li><strong>Step {step_num}:</strong> {content}")

            if substeps:
                content_parts.append("<ol type='a'>")
                for substep in substeps:
                    content_parts.append(f"<li>{substep}</li>")
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
            content_parts.append(f"<li>The purpose of this procedure is: {sop_content.purpose[:150]}</li>")

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
