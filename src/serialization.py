"""Rebuild ``SOPContent`` / ``TrainingModule`` / ``Assessment`` objects from the
dictionaries their own ``to_dict()`` methods produce.

This module exists so the SME review workflow in ``app.py`` can persist a job's
generated content as plain JSON (``job.json``) and, after a reviewer edits it,
turn the edited JSON back into the real model objects that ``SCORMExporter``
and the transparency report already know how to render - without changing any
of those classes.

Every function tolerates missing keys by falling back to the class's own
default for that field, so a hand-edited or partial dict still rebuilds a
sane object. Round-trip: ``obj.to_dict() == xxx_from_dict(obj.to_dict()).to_dict()``.
"""

import copy
from typing import Dict, Optional

from .parser import SOPContent
from .generator import TrainingModule
from .assessments import Assessment, Question


def _restore_unlisted_fields(obj, data: Dict) -> None:
    """Copy every key of ``data`` that names an attribute of ``obj``.

    The model classes' ``to_dict()`` contracts are append-only (CLAUDE.md), so
    fields added after this module was written - ``SOPContent.provenance`` and
    ``lines``, ``TrainingModule.objectives`` - must survive a round-trip without
    this module having to learn about each one. The explicit assignments that
    follow in each rebuilder then overwrite the well-known fields with their
    normalised forms.
    """
    for key, value in data.items():
        if hasattr(obj, key):
            setattr(obj, key, copy.deepcopy(value))


def sop_from_dict(data: Optional[Dict]) -> SOPContent:
    """Rebuild a :class:`SOPContent` from its ``to_dict()`` output."""
    data = data or {}
    sop = SOPContent()
    _restore_unlisted_fields(sop, data)
    sop.title = data.get("title", sop.title)
    sop.version = data.get("version", sop.version)
    sop.effective_date = data.get("effective_date", sop.effective_date)
    sop.purpose = data.get("purpose", sop.purpose)
    sop.scope = data.get("scope", sop.scope)
    sop.responsibilities = list(data.get("responsibilities") or [])
    # Procedures are already plain dicts in to_dict(); keep them as-is so any
    # keys future parser versions add (append-only contract) survive a
    # round-trip even though this module doesn't know about them.
    sop.procedures = [dict(p) for p in (data.get("procedures") or [])]
    sop.safety_warnings = list(data.get("safety_warnings") or [])
    sop.definitions = dict(data.get("definitions") or {})
    sop.references = list(data.get("references") or [])
    sop.raw_content = data.get("raw_content", sop.raw_content)
    return sop


def module_from_dict(data: Optional[Dict]) -> TrainingModule:
    """Rebuild a :class:`TrainingModule` from its ``to_dict()`` output."""
    data = data or {}
    module = TrainingModule()
    _restore_unlisted_fields(module, data)
    module.title = data.get("title", module.title)
    module.learning_objectives = list(data.get("learning_objectives") or [])
    module.sections = [dict(s) for s in (data.get("sections") or [])]
    module.estimated_duration = data.get("estimated_duration", module.estimated_duration)
    module.prerequisites = list(data.get("prerequisites") or [])
    module.summary = data.get("summary", module.summary)
    return module


def question_from_dict(data: Optional[Dict]) -> Question:
    """Rebuild a :class:`Question` from its ``to_dict()`` output.

    ``answer_hash`` is not restored directly - it is a computed property
    derived from ``salt``, ``options`` and ``correct_answer`` - so a question
    whose correct answer or options were edited automatically gets the right
    hash for its *new* content, using the *original* per-question salt.
    """
    data = data or {}
    return Question(
        question_id=data.get("id", ""),
        question_type=data.get("type", ""),
        question_text=data.get("text", ""),
        options=list(data.get("options") or []),
        correct_answer=data.get("correct_answer"),
        explanation=data.get("explanation", ""),
        points=data.get("points", 1),
        salt=data.get("salt", ""),
        source_ref=dict(data.get("source_ref") or {}),
        topic=data.get("topic", ""),
    )


def assessment_from_dict(data: Optional[Dict]) -> Assessment:
    """Rebuild an :class:`Assessment` (and its :class:`Question`\\ s) from its
    ``to_dict()`` output."""
    data = data or {}
    assessment = Assessment()
    assessment.title = data.get("title", assessment.title)
    assessment.description = data.get("description", assessment.description)
    assessment.questions = [question_from_dict(q) for q in (data.get("questions") or [])]
    assessment.passing_score = data.get("passing_score", assessment.passing_score)
    assessment.time_limit = data.get("time_limit", assessment.time_limit)
    assessment.randomize_questions = data.get(
        "randomize_questions", assessment.randomize_questions)
    assessment.randomize_options = data.get(
        "randomize_options", assessment.randomize_options)
    assessment.requested_questions = data.get(
        "requested_questions", len(assessment.questions))
    assessment.notes = list(data.get("notes") or [])
    return assessment
