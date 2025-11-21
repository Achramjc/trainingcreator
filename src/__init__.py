"""
Training Creator - Convert SOPs and work instructions into LMS-ready training materials
"""

__version__ = "1.0.0"
__author__ = "Training Creator Team"

from .parser import SOPParser
from .generator import TrainingGenerator
from .assessments import AssessmentGenerator
from .scorm_exporter import SCORMExporter

__all__ = [
    "SOPParser",
    "TrainingGenerator",
    "AssessmentGenerator",
    "SCORMExporter",
]
