"""
SOP Parser - Extract content from various document formats
"""

import os
import re
from typing import Dict, List, Optional
from pathlib import Path

import markdown


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
        }


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

    def _extract_structure(self, content: str, original_content: str = None) -> SOPContent:
        """
        Extract structured information from text content
        Uses pattern matching to identify common SOP sections
        """
        sop = SOPContent()
        sop.raw_content = original_content or content

        lines = content.split('\n')

        # Extract title (usually first non-empty line or line with "SOP" or "Procedure")
        for line in lines[:10]:
            line = line.strip()
            if line and (not sop.title or 'sop' in line.lower() or 'procedure' in line.lower()):
                sop.title = line
                break

        # Extract version
        version_match = re.search(r'version[:\s]+([0-9.]+)', content, re.IGNORECASE)
        if version_match:
            sop.version = version_match.group(1)

        # Extract effective date
        date_match = re.search(r'effective\s+date[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', content, re.IGNORECASE)
        if date_match:
            sop.effective_date = date_match.group(1)

        # Extract purpose section
        purpose_match = re.search(r'(?:purpose|objective)[:\s]+(.*?)(?:\n\n|\n[A-Z])', content, re.IGNORECASE | re.DOTALL)
        if purpose_match:
            sop.purpose = purpose_match.group(1).strip()

        # Extract scope section
        scope_match = re.search(r'scope[:\s]+(.*?)(?:\n\n|\n[A-Z])', content, re.IGNORECASE | re.DOTALL)
        if scope_match:
            sop.scope = scope_match.group(1).strip()

        # Extract procedures (numbered steps)
        procedures = self._extract_procedures(content)
        sop.procedures = procedures

        # Extract safety warnings
        safety_warnings = re.findall(r'(?:warning|caution|danger)[:\s]+(.*?)(?:\n|$)', content, re.IGNORECASE)
        sop.safety_warnings = [w.strip() for w in safety_warnings]

        # Extract definitions
        definitions = self._extract_definitions(content)
        sop.definitions = definitions

        return sop

    def _extract_procedures(self, content: str) -> List[Dict[str, any]]:
        """Extract numbered procedure steps"""
        procedures = []

        # Match numbered steps like "1.", "1)", "Step 1:", etc.
        step_pattern = r'(?:^|\n)(?:step\s+)?(\d+)[.):]\s+(.*?)(?=(?:\n(?:step\s+)?\d+[.):]|\n\n|$))'
        matches = re.finditer(step_pattern, content, re.IGNORECASE | re.DOTALL | re.MULTILINE)

        for match in matches:
            step_num = match.group(1)
            step_content = match.group(2).strip()

            # Check for sub-steps
            substeps = re.findall(r'[a-z][.)][\s]+(.+?)(?=\n[a-z][.):]|\n\n|$)', step_content, re.DOTALL)

            procedures.append({
                "step_number": step_num,
                "content": step_content,
                "substeps": substeps if substeps else []
            })

        return procedures

    def _extract_definitions(self, content: str) -> Dict[str, str]:
        """Extract term definitions"""
        definitions = {}

        # Look for definitions section
        def_section = re.search(r'definitions?[:\s]+(.*?)(?:\n\n[A-Z]|\Z)', content, re.IGNORECASE | re.DOTALL)
        if def_section:
            def_text = def_section.group(1)
            # Match patterns like "Term: definition" or "Term - definition"
            def_matches = re.findall(r'([A-Za-z\s]+)[\s]*[:-][\s]*(.+?)(?=\n[A-Z]|\n\n|$)', def_text)
            for term, definition in def_matches:
                definitions[term.strip()] = definition.strip()

        return definitions
