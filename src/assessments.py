"""
Assessment Generator - Create verification questions and quizzes from SOP content
"""

import random
import re
from typing import Dict, List, Optional
from .parser import SOPContent


class Question:
    """Represents a single assessment question"""

    def __init__(self, question_id: str, question_type: str, question_text: str,
                 options: List[str] = None, correct_answer: any = None,
                 explanation: str = "", points: int = 1):
        self.id = question_id
        self.type = question_type  # multiple_choice, true_false, fill_blank, ordering
        self.text = question_text
        self.options = options or []
        self.correct_answer = correct_answer
        self.explanation = explanation
        self.points = points

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "options": self.options,
            "correct_answer": self.correct_answer,
            "explanation": self.explanation,
            "points": self.points
        }


class Assessment:
    """Collection of assessment questions"""

    def __init__(self):
        self.title: str = ""
        self.description: str = ""
        self.questions: List[Question] = []
        self.passing_score: int = 70  # Percentage
        self.time_limit: Optional[int] = None  # Minutes
        self.randomize_questions: bool = False
        self.randomize_options: bool = True

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            "title": self.title,
            "description": self.description,
            "questions": [q.to_dict() for q in self.questions],
            "passing_score": self.passing_score,
            "time_limit": self.time_limit,
            "randomize_questions": self.randomize_questions,
            "randomize_options": self.randomize_options
        }


class AssessmentGenerator:
    """Generate assessment questions from SOP content"""

    def __init__(self):
        self.question_templates = self._load_question_templates()

    def generate(self, sop_content: SOPContent, num_questions: int = 5,
                 passing_score: int = 70) -> Assessment:
        """
        Generate an assessment from SOP content

        Args:
            sop_content: Parsed SOP content
            num_questions: Number of questions to generate
            passing_score: Minimum passing score percentage

        Returns:
            Assessment object with generated questions
        """
        assessment = Assessment()
        assessment.title = f"{sop_content.title} - Verification Assessment"
        assessment.description = "Complete this assessment to verify your understanding of the procedure."
        assessment.passing_score = passing_score

        # Generate different types of questions
        questions = []

        # Generate questions from purpose and scope
        if sop_content.purpose:
            questions.extend(self._generate_purpose_questions(sop_content))

        # Generate questions from procedures
        if sop_content.procedures:
            questions.extend(self._generate_procedure_questions(sop_content))

        # Generate questions from safety warnings
        if sop_content.safety_warnings:
            questions.extend(self._generate_safety_questions(sop_content))

        # Generate questions from definitions
        if sop_content.definitions:
            questions.extend(self._generate_definition_questions(sop_content))

        # Select the requested number of questions
        if len(questions) > num_questions:
            questions = random.sample(questions, num_questions)

        assessment.questions = questions

        return assessment

    def _load_question_templates(self) -> Dict:
        """Load question generation templates"""
        return {
            "purpose": [
                "What is the primary purpose of this procedure?",
                "Why is this procedure important?",
                "What does this procedure aim to accomplish?"
            ],
            "scope": [
                "When should this procedure be applied?",
                "What situations does this procedure cover?",
                "Who is responsible for following this procedure?"
            ],
            "procedure": [
                "What is the correct order of steps?",
                "What should you do in step {step_num}?",
                "Which step involves {action}?"
            ],
            "safety": [
                "Which of the following is a safety warning for this procedure?",
                "What precautions should be taken during this procedure?",
                "True or False: {warning}"
            ]
        }

    def _generate_purpose_questions(self, sop_content: SOPContent) -> List[Question]:
        """Generate questions about the purpose and scope"""
        questions = []
        q_id = 1

        if sop_content.purpose:
            # Multiple choice question about purpose
            question = Question(
                question_id=f"q{q_id}",
                question_type="multiple_choice",
                question_text="What is the primary purpose of this procedure?",
                options=[
                    sop_content.purpose[:100],
                    "To ensure compliance with regulatory requirements only",
                    "To document historical processes",
                    "To increase paperwork requirements"
                ],
                correct_answer=0,
                explanation=f"The stated purpose is: {sop_content.purpose}",
                points=1
            )
            questions.append(question)
            q_id += 1

        if sop_content.scope:
            # True/False question about scope
            question = Question(
                question_id=f"q{q_id}",
                question_type="true_false",
                question_text=f"This procedure applies in the following context: {sop_content.scope[:100]}",
                options=["True", "False"],
                correct_answer=0,
                explanation=f"The scope of this procedure is: {sop_content.scope}",
                points=1
            )
            questions.append(question)
            q_id += 1

        return questions

    def _generate_procedure_questions(self, sop_content: SOPContent) -> List[Question]:
        """Generate questions about procedure steps"""
        questions = []
        q_id = len([q for q in questions]) + 1

        if len(sop_content.procedures) >= 2:
            # Question about step ordering
            steps_sample = random.sample(sop_content.procedures, min(4, len(sop_content.procedures)))
            correct_order = sorted(steps_sample, key=lambda x: int(x.get('step_number', 0)))

            question = Question(
                question_id=f"q{q_id}",
                question_type="ordering",
                question_text="What is the correct order of these procedure steps?",
                options=[f"Step {s.get('step_number')}: {s.get('content', '')[:60]}..." for s in steps_sample],
                correct_answer=[int(s.get('step_number', 0)) for s in correct_order],
                explanation="Steps must be performed in the specified sequence.",
                points=2
            )
            questions.append(question)
            q_id += 1

        # Generate questions about specific steps
        for proc in random.sample(sop_content.procedures, min(3, len(sop_content.procedures))):
            step_num = proc.get('step_number', '')
            content = proc.get('content', '')

            if len(content) > 20:
                # Multiple choice question about what happens in this step
                question = Question(
                    question_id=f"q{q_id}",
                    question_type="multiple_choice",
                    question_text=f"What should be done in Step {step_num}?",
                    options=[
                        content[:100],
                        "Skip this step if time is limited",
                        "Contact supervisor and wait",
                        "Document the issue and move to next step"
                    ],
                    correct_answer=0,
                    explanation=f"Step {step_num} requires: {content}",
                    points=1
                )
                questions.append(question)
                q_id += 1

        return questions

    def _generate_safety_questions(self, sop_content: SOPContent) -> List[Question]:
        """Generate questions about safety warnings"""
        questions = []
        q_id = 100  # Start at 100 to avoid conflicts

        for idx, warning in enumerate(sop_content.safety_warnings[:3], 1):
            # True/False questions about safety
            question = Question(
                question_id=f"q{q_id}",
                question_type="true_false",
                question_text=f"Safety Warning: {warning}",
                options=["True - This is a valid safety warning", "False - This is not a concern"],
                correct_answer=0,
                explanation="All stated safety warnings must be followed.",
                points=2  # Safety questions worth more points
            )
            questions.append(question)
            q_id += 1

        if len(sop_content.safety_warnings) >= 2:
            # Multiple choice about which is NOT a safety warning
            fake_warnings = [
                "Wearing safety equipment is optional",
                "Speed is more important than accuracy",
                "Safety checks can be skipped if in a hurry"
            ]

            selected_fake = random.choice(fake_warnings)
            options = sop_content.safety_warnings[:3] + [selected_fake]
            random.shuffle(options)

            correct_idx = options.index(selected_fake)

            question = Question(
                question_id=f"q{q_id}",
                question_type="multiple_choice",
                question_text="Which of the following is NOT a valid safety warning for this procedure?",
                options=options,
                correct_answer=correct_idx,
                explanation="Always follow all safety warnings and never take shortcuts.",
                points=2
            )
            questions.append(question)
            q_id += 1

        return questions

    def _generate_definition_questions(self, sop_content: SOPContent) -> List[Question]:
        """Generate questions about terminology and definitions"""
        questions = []
        q_id = 200  # Start at 200 to avoid conflicts

        for term, definition in list(sop_content.definitions.items())[:3]:
            # Fill in the blank or multiple choice about definitions
            question = Question(
                question_id=f"q{q_id}",
                question_type="multiple_choice",
                question_text=f"What is the definition of '{term}'?",
                options=[
                    definition,
                    "A general industry term",
                    "Not defined in this procedure",
                    "Optional terminology"
                ],
                correct_answer=0,
                explanation=f"{term} is defined as: {definition}",
                points=1
            )
            questions.append(question)
            q_id += 1

        return questions


class MedicalDeviceAssessmentGenerator(AssessmentGenerator):
    """Assessment generator specifically for medical device training"""

    def __init__(self):
        super().__init__()
        self.min_passing = 80

    def generate(self, sop_content, num_questions=5, passing_score=80):
        """
        Generate assessment with medical device specific questions

        Args:
            sop_content: Parsed SOP content
            num_questions: Total number of questions (includes 2 required medical device questions)
            passing_score: Minimum passing score (will be raised to 80 if lower)

        Returns:
            Assessment object with medical device compliance questions
        """

        # Get base assessment (request fewer to make room for required questions)
        assessment = super().generate(sop_content, max(1, num_questions - 2), passing_score)

        # Add required medical device questions
        assessment.questions.extend(self._add_required_medical_questions())

        # Ensure minimum passing score for medical device training
        if assessment.passing_score < 80:
            assessment.passing_score = 80

        # Update description to indicate medical device compliance
        assessment.description = "Complete this assessment to verify your understanding of the procedure. " + \
                               "This assessment includes compliance questions required for medical device manufacturing."

        return assessment

    def _add_required_medical_questions(self):
        """
        Add questions that FDA expects to see in all medical device training

        These questions ensure that trainees understand:
        1. Proper deviation handling per 21 CFR 820.70
        2. Impact awareness per 21 CFR 820.25

        Returns:
            List of required Question objects
        """
        required = []

        # Deviation handling question (21 CFR 820.70 - Production and Process Controls)
        q1 = Question(
            question_id="md_req_1",
            question_type="multiple_choice",
            question_text="What should you do if you cannot follow this procedure as written?",
            options=[
                "Stop work and notify supervisor/QA for deviation approval",
                "Continue with best judgment and document later",
                "Skip the step if it seems unnecessary",
                "Ask a coworker what they would do"
            ],
            correct_answer=0,
            explanation="All deviations must be approved and documented per 21 CFR 820.70. "
                       "Unauthorized deviations can compromise product quality and patient safety.",
            points=2
        )
        required.append(q1)

        # Quality impact question (21 CFR 820.25 - Personnel requirements)
        q2 = Question(
            question_id="md_req_2",
            question_type="true_false",
            question_text="Deviations from this procedure could potentially impact product quality or patient safety.",
            options=["True", "False"],
            correct_answer=0,
            explanation="All procedures in a Quality Management System can impact product quality. "
                       "Per 21 CFR 820.25, personnel must be trained to understand how their work affects quality.",
            points=2
        )
        required.append(q2)

        return required
