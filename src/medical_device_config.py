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

    # Questions every medical device training module must contain.  This is the
    # single source of truth for their wording: MedicalDeviceAssessmentGenerator
    # builds its required questions straight from here.
    #
    # The distractors are deliberately *plausible* - they are the wrong things
    # people actually do on a production floor (press on using judgement, follow
    # the revision you were trained on, ask a colleague).  A distractor that is
    # obviously silly teaches nothing and lets an untrained operator pass, which
    # is exactly the failure mode this assessment exists to catch.
    'required_questions': [
        {
            'id': 'md_req_1',
            'type': 'multiple_choice',
            'points': 2,
            'reference': '21 CFR 820.70',
            'text': 'What should you do if you cannot follow this procedure as written?',
            'correct': 'Stop work and notify your supervisor or QA so the deviation '
                       'can be approved and documented before you continue',
            'distractors': [
                'Complete the work using your best judgement and record what you '
                'did in the batch record afterwards',
                'Follow the version of the procedure you were originally trained '
                'on, since that one is known to work',
                'Ask an experienced colleague how they normally handle it and '
                'follow the approach they describe',
            ],
            'explanation': 'All deviations must be approved and documented per '
                           '21 CFR 820.70. Unauthorized deviations can compromise '
                           'product quality and patient safety.',
        },
        {
            'id': 'md_req_2',
            'type': 'true_false',
            'points': 2,
            'reference': '21 CFR 820.25',
            'text': 'Deviations from this procedure could potentially impact '
                    'product quality or patient safety.',
            'correct': True,
            'explanation': 'All procedures in a Quality Management System can impact '
                           'product quality. Per 21 CFR 820.25, personnel must be '
                           'trained to understand how their work affects quality.',
        }
    ],

    # What the SCORM package is allowed to tell the learner's browser about the
    # answer key, and what that does and does not protect against.  Surfaced in
    # metadata.json so an auditor sees the limitation rather than discovering it.
    'assessment_integrity': {
        'answer_key_in_package': 'salted SHA-256 hash of the normalised correct '
                                 'option text only',
        'plaintext_key_shipped': False,
        'client_side_scoring': True,
        'known_limitation': 'A SCORM package is static content with no server. A '
                            'learner with browser developer tools can hash each of '
                            'the 2-4 displayed options against the published salt '
                            'and identify the correct one. This design prevents the '
                            'answer key being read from the page source; it does not '
                            'prevent deliberate tampering.',
        'planned_remediation': 'Server- or LMS-verified scoring, so the answer key '
                               'never leaves the grader.',
    },

    'transparency_metadata': {
        'tool_name': 'Training Creator Pro - Training Development Tool',
        'validation_status': 'Not a validated system - Content requires SME review',
        'compliance_note': 'LMS maintains all training records per 21 CFR 820.25'
    }
}
