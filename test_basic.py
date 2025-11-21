"""
Basic test to verify the training creator works without PDF dependencies
"""

from src.parser import SOPParser
from src.generator import TrainingGenerator
from src.assessments import AssessmentGenerator
from src.scorm_exporter import SCORMExporter

print("=" * 60)
print("Testing Training Creator")
print("=" * 60)

# Test 1: Parse text SOP
print("\n1. Parsing example SOP...")
parser = SOPParser()
sop = parser.parse('examples/sample_sop.txt')
print(f"   ✓ Parsed: {sop.title}")
print(f"   - Version: {sop.version}")
print(f"   - Procedures: {len(sop.procedures)} steps")
print(f"   - Safety Warnings: {len(sop.safety_warnings)}")
print(f"   - Definitions: {len(sop.definitions)}")

# Test 2: Generate training content
print("\n2. Generating training content...")
generator = TrainingGenerator()
training = generator.generate(sop)
print(f"   ✓ Generated training module")
print(f"   - Learning Objectives: {len(training.learning_objectives)}")
print(f"   - Sections: {len(training.sections)}")
print(f"   - Estimated Duration: {training.estimated_duration} minutes")

# Test 3: Generate assessment
print("\n3. Generating assessment...")
assessment_gen = AssessmentGenerator()
assessment = assessment_gen.generate(sop, num_questions=5)
print(f"   ✓ Generated assessment")
print(f"   - Questions: {len(assessment.questions)}")
print(f"   - Passing Score: {assessment.passing_score}%")

# Test 4: Export to SCORM
print("\n4. Exporting to SCORM package...")
exporter = SCORMExporter(scorm_version="1.2")
package_path = exporter.create_package(training, assessment, 'test_output', 'emergency_shutdown_training')
print(f"   ✓ SCORM package created: {package_path}")

print("\n" + "=" * 60)
print("✅ All tests passed!")
print("=" * 60)
print("\nYou can upload the SCORM package to your LMS:")
print(f"   {package_path}")
