"""
Configuration for medical device training generation

This module provides medical device-specific configuration for training generation
aligned with ISO 13485 and 21 CFR 820 requirements.
"""

MEDICAL_DEVICE_CONFIG = {
    'default_passing_score': 80,  # FDA expectation
    'require_effectiveness_check': True,
    'draft_watermark': True,  # Always show content is draft

    'sop_categories': {
        'GMP': ['Manufacturing', 'Packaging', 'Labeling', 'Storage'],
        'QMS': ['Document Control', 'Management Review', 'CAPA', 'Training'],
        'Design': ['Design Controls', 'Verification', 'Validation', 'Design Transfer'],
        'Production': ['Process Control', 'Equipment', 'Process Validation', 'Monitoring'],
        'Quality': ['Inspection', 'Testing', 'Nonconforming Product', 'Complaints']
    },

    'required_questions': [
        {
            'text': 'What should you do if you cannot follow this procedure as written?',
            'correct': 'Stop work and notify supervisor/QA for deviation approval',
            'type': 'multiple_choice'
        },
        {
            'text': 'Can deviations from this procedure impact product quality or patient safety?',
            'correct': True,
            'type': 'true_false'
        }
    ],

    'transparency_metadata': {
        'tool_name': 'Training Creator Pro - Training Development Tool',
        'validation_status': 'Not a validated system - Content requires SME review',
        'compliance_note': 'LMS maintains all training records per 21 CFR 820.25'
    }
}
